//! FUSE filesystem layer for transparent object storage
//!
//! ## Architecture
//!
//! - `cache`: In-memory page cache with tri-state pages (Clean/Dirty/Syncing)
//! - `dirty_tracker`: Simple file-level tracking of modified files
//! - `filesystem`: FUSE filesystem implementation
//! - `sync_worker`: Background worker for syncing dirty files to object storage

pub mod cache;
pub mod dirty_tracker;
pub mod filesystem;
pub mod sync_worker;

pub use cache::{
    DirtyFile, FileCache, FileMetadata, FileSnapshot, Page, PageCache, PageState, PAGE_SIZE,
};
pub use dirty_tracker::{DirtyFileTracker, DirtyPageTracker};
pub use filesystem::{BasilicaFS, SharedBasilicaFS};
pub use sync_worker::{
    SyncStats, SyncWorker, DEFAULT_MAX_CONCURRENT_UPLOADS, DEFAULT_QUIET_PERIOD_MS,
};
