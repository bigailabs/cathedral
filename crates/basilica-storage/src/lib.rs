//! Basilica Storage Layer
//!
//! Provides persistent storage for stateful jobs using object storage backends (S3, R2, GCS).
//!
//! ## Architecture
//!
//! This crate implements a snapshot-on-pause approach for MVP:
//! - Jobs write to local disk during execution
//! - On pause/suspend, we snapshot the working directory to object storage
//! - On resume, we restore from the snapshot
//!
//! Future: Full FUSE-based mmap-to-object-storage translation for real-time sync.

pub mod backend;
pub mod snapshot;
pub mod config;
pub mod error;

pub use backend::{StorageBackend, ObjectStoreBackend};
pub use snapshot::{SnapshotManager, SnapshotMetadata};
pub use config::StorageConfig;
pub use error::{StorageError, Result};
