//! Per-namespace metrics tracking for deployment progress feedback.
//!
//! Provides lock-free concurrent metrics tracking for storage operations
//! on a per-namespace basis, enabling granular visibility into storage sync progress.

use chrono::{DateTime, Utc};
use parking_lot::RwLock;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

/// Metrics for a single namespace's storage operations.
#[derive(Debug)]
pub struct NamespaceMetrics {
    pub bytes_written: AtomicU64,
    pub bytes_read: AtomicU64,
    pub files_created: AtomicU64,
    pub files_modified: AtomicU64,
    last_write_at: RwLock<Option<DateTime<Utc>>>,
    last_read_at: RwLock<Option<DateTime<Utc>>>,
    active_writes: AtomicU64,
    active_reads: AtomicU64,
}

impl Default for NamespaceMetrics {
    fn default() -> Self {
        Self::new()
    }
}

impl NamespaceMetrics {
    pub fn new() -> Self {
        Self {
            bytes_written: AtomicU64::new(0),
            bytes_read: AtomicU64::new(0),
            files_created: AtomicU64::new(0),
            files_modified: AtomicU64::new(0),
            last_write_at: RwLock::new(None),
            last_read_at: RwLock::new(None),
            active_writes: AtomicU64::new(0),
            active_reads: AtomicU64::new(0),
        }
    }

    pub fn record_write(&self, bytes: u64) {
        self.bytes_written.fetch_add(bytes, Ordering::Relaxed);
        *self.last_write_at.write() = Some(Utc::now());
    }

    pub fn record_read(&self, bytes: u64) {
        self.bytes_read.fetch_add(bytes, Ordering::Relaxed);
        *self.last_read_at.write() = Some(Utc::now());
    }

    pub fn record_file_created(&self) {
        self.files_created.fetch_add(1, Ordering::Relaxed);
    }

    pub fn record_file_modified(&self) {
        self.files_modified.fetch_add(1, Ordering::Relaxed);
    }

    pub fn start_write(&self) {
        self.active_writes.fetch_add(1, Ordering::Relaxed);
    }

    pub fn end_write(&self) {
        let _ = self
            .active_writes
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |current| {
                current.checked_sub(1)
            });
    }

    pub fn start_read(&self) {
        self.active_reads.fetch_add(1, Ordering::Relaxed);
    }

    pub fn end_read(&self) {
        let _ = self
            .active_reads
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |current| {
                current.checked_sub(1)
            });
    }

    pub fn get_bytes_written(&self) -> u64 {
        self.bytes_written.load(Ordering::Relaxed)
    }

    pub fn get_bytes_read(&self) -> u64 {
        self.bytes_read.load(Ordering::Relaxed)
    }

    pub fn get_files_created(&self) -> u64 {
        self.files_created.load(Ordering::Relaxed)
    }

    pub fn get_files_modified(&self) -> u64 {
        self.files_modified.load(Ordering::Relaxed)
    }

    pub fn has_active_writes(&self) -> bool {
        self.active_writes.load(Ordering::Relaxed) > 0
    }

    pub fn has_active_reads(&self) -> bool {
        self.active_reads.load(Ordering::Relaxed) > 0
    }

    pub fn get_last_write_at(&self) -> Option<DateTime<Utc>> {
        *self.last_write_at.read()
    }

    pub fn get_last_read_at(&self) -> Option<DateTime<Utc>> {
        *self.last_read_at.read()
    }

    pub fn get_active_write_count(&self) -> u64 {
        self.active_writes.load(Ordering::Relaxed)
    }

    pub fn get_active_read_count(&self) -> u64 {
        self.active_reads.load(Ordering::Relaxed)
    }
}

/// Thread-safe store for per-namespace metrics using DashMap for lock-free concurrent access.
#[derive(Clone)]
pub struct PerNamespaceMetricsStore {
    inner: Arc<dashmap::DashMap<String, Arc<NamespaceMetrics>>>,
}

impl Default for PerNamespaceMetricsStore {
    fn default() -> Self {
        Self::new()
    }
}

impl PerNamespaceMetricsStore {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(dashmap::DashMap::with_capacity(256)),
        }
    }

    pub fn get_or_create(&self, namespace: &str) -> Arc<NamespaceMetrics> {
        if let Some(metrics) = self.inner.get(namespace) {
            return Arc::clone(metrics.value());
        }
        self.inner
            .entry(namespace.to_owned())
            .or_insert_with(|| Arc::new(NamespaceMetrics::new()))
            .clone()
    }

    pub fn get(&self, namespace: &str) -> Option<Arc<NamespaceMetrics>> {
        self.inner.get(namespace).map(|m| Arc::clone(m.value()))
    }

    pub fn list_namespaces(&self) -> Vec<String> {
        self.inner.iter().map(|e| e.key().clone()).collect()
    }

    pub fn remove(&self, namespace: &str) -> Option<Arc<NamespaceMetrics>> {
        self.inner.remove(namespace).map(|(_, v)| v)
    }

    pub fn namespace_count(&self) -> usize {
        self.inner.len()
    }
}

/// Snapshot of namespace metrics for API responses.
#[derive(Debug, Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct NamespaceMetricsSnapshot {
    pub namespace: String,
    pub bytes_written: u64,
    pub bytes_read: u64,
    pub files_created: u64,
    pub files_modified: u64,
    pub last_write_at: Option<String>,
    pub last_read_at: Option<String>,
    pub active_writes: bool,
    pub active_reads: bool,
}

impl NamespaceMetricsSnapshot {
    pub fn from_metrics(namespace: &str, metrics: &NamespaceMetrics) -> Self {
        Self {
            namespace: namespace.to_string(),
            bytes_written: metrics.get_bytes_written(),
            bytes_read: metrics.get_bytes_read(),
            files_created: metrics.get_files_created(),
            files_modified: metrics.get_files_modified(),
            last_write_at: metrics.get_last_write_at().map(|dt| dt.to_rfc3339()),
            last_read_at: metrics.get_last_read_at().map(|dt| dt.to_rfc3339()),
            active_writes: metrics.has_active_writes(),
            active_reads: metrics.has_active_reads(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_namespace_metrics_default() {
        let metrics = NamespaceMetrics::new();
        assert_eq!(metrics.get_bytes_written(), 0);
        assert_eq!(metrics.get_bytes_read(), 0);
        assert_eq!(metrics.get_files_created(), 0);
        assert!(!metrics.has_active_writes());
        assert!(!metrics.has_active_reads());
    }

    #[test]
    fn test_namespace_metrics_record_write() {
        let metrics = NamespaceMetrics::new();
        metrics.record_write(1024);
        assert_eq!(metrics.get_bytes_written(), 1024);
        metrics.record_write(2048);
        assert_eq!(metrics.get_bytes_written(), 3072);
        assert!(metrics.get_last_write_at().is_some());
    }

    #[test]
    fn test_namespace_metrics_record_read() {
        let metrics = NamespaceMetrics::new();
        metrics.record_read(512);
        assert_eq!(metrics.get_bytes_read(), 512);
        assert!(metrics.get_last_read_at().is_some());
    }

    #[test]
    fn test_namespace_metrics_active_writes() {
        let metrics = NamespaceMetrics::new();
        assert!(!metrics.has_active_writes());
        metrics.start_write();
        assert!(metrics.has_active_writes());
        assert_eq!(metrics.get_active_write_count(), 1);
        metrics.start_write();
        assert_eq!(metrics.get_active_write_count(), 2);
        metrics.end_write();
        assert_eq!(metrics.get_active_write_count(), 1);
        metrics.end_write();
        assert!(!metrics.has_active_writes());
    }

    #[test]
    fn test_per_namespace_store_get_or_create() {
        let store = PerNamespaceMetricsStore::new();
        let metrics1 = store.get_or_create("u-test1");
        metrics1.record_write(100);

        let metrics1_again = store.get_or_create("u-test1");
        assert_eq!(metrics1_again.get_bytes_written(), 100);

        let metrics2 = store.get_or_create("u-test2");
        assert_eq!(metrics2.get_bytes_written(), 0);
    }

    #[test]
    fn test_per_namespace_store_list_namespaces() {
        let store = PerNamespaceMetricsStore::new();
        store.get_or_create("u-test1");
        store.get_or_create("u-test2");

        let namespaces = store.list_namespaces();
        assert_eq!(namespaces.len(), 2);
        assert!(namespaces.contains(&"u-test1".to_string()));
        assert!(namespaces.contains(&"u-test2".to_string()));
    }

    #[test]
    fn test_per_namespace_store_remove() {
        let store = PerNamespaceMetricsStore::new();
        store.get_or_create("u-test1");
        assert_eq!(store.namespace_count(), 1);

        let removed = store.remove("u-test1");
        assert!(removed.is_some());
        assert_eq!(store.namespace_count(), 0);
        assert!(store.get("u-test1").is_none());
    }

    #[test]
    fn test_namespace_metrics_snapshot() {
        let metrics = NamespaceMetrics::new();
        metrics.record_write(1024);
        metrics.record_read(512);
        metrics.record_file_created();
        metrics.start_write();

        let snapshot = NamespaceMetricsSnapshot::from_metrics("u-test", &metrics);
        assert_eq!(snapshot.namespace, "u-test");
        assert_eq!(snapshot.bytes_written, 1024);
        assert_eq!(snapshot.bytes_read, 512);
        assert_eq!(snapshot.files_created, 1);
        assert!(snapshot.active_writes);
        assert!(!snapshot.active_reads);
    }
}
