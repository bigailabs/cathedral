//! Basilica Storage Layer
//!
//! Provides persistent storage for stateful jobs using object storage backends (S3, R2, GCS).
//!
//! ## Architecture
//!
//! ### FUSE Filesystem (Production)
//! - Transparent file I/O backed by object storage
//! - In-memory caching with background sync
//! - Continuous protection (syncs every 1 second)
//! - Zero code changes for users
//! - Supports mmap for numpy/PyTorch
//!
//! ### Snapshot Manager (Legacy/Testing)
//! - Manual snapshot-on-pause approach
//! - For testing and backwards compatibility
//! - Use FUSE for production workloads

pub mod backend;
pub mod config;
pub mod error;
pub mod fuse;
pub mod snapshot;

pub use backend::{ObjectStoreBackend, StorageBackend};
pub use config::StorageConfig;
pub use error::{Result, StorageError};
pub use fuse::{BasilicaFS, DirtyPageTracker, PageCache, SyncWorker};
pub use snapshot::{SnapshotManager, SnapshotMetadata};
