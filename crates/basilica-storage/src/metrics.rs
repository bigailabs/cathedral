use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

#[derive(Debug, Clone)]
pub struct StorageMetrics {
    pub reads: Arc<AtomicU64>,
    pub writes: Arc<AtomicU64>,
    pub bytes_read: Arc<AtomicU64>,
    pub bytes_written: Arc<AtomicU64>,
    pub cache_hits: Arc<AtomicU64>,
    pub cache_misses: Arc<AtomicU64>,
    pub quota_exceeded: Arc<AtomicU64>,
    pub mounts_created: Arc<AtomicU64>,
    pub mounts_destroyed: Arc<AtomicU64>,
    pub active_mounts: Arc<AtomicU64>,
}

impl StorageMetrics {
    pub fn new() -> Self {
        Self {
            reads: Arc::new(AtomicU64::new(0)),
            writes: Arc::new(AtomicU64::new(0)),
            bytes_read: Arc::new(AtomicU64::new(0)),
            bytes_written: Arc::new(AtomicU64::new(0)),
            cache_hits: Arc::new(AtomicU64::new(0)),
            cache_misses: Arc::new(AtomicU64::new(0)),
            quota_exceeded: Arc::new(AtomicU64::new(0)),
            mounts_created: Arc::new(AtomicU64::new(0)),
            mounts_destroyed: Arc::new(AtomicU64::new(0)),
            active_mounts: Arc::new(AtomicU64::new(0)),
        }
    }

    pub fn record_mount_created(&self) {
        self.mounts_created.fetch_add(1, Ordering::Relaxed);
        self.active_mounts.fetch_add(1, Ordering::Relaxed);
    }

    pub fn record_mount_destroyed(&self) {
        self.mounts_destroyed.fetch_add(1, Ordering::Relaxed);
        self.active_mounts.fetch_sub(1, Ordering::Relaxed);
    }

    pub fn get_active_mounts(&self) -> u64 {
        self.active_mounts.load(Ordering::Relaxed)
    }
}

impl Default for StorageMetrics {
    fn default() -> Self {
        Self::new()
    }
}
