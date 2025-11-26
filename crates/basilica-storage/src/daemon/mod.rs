//! Multi-tenant FUSE daemon module.
//!
//! This module provides the DaemonSet-based multi-tenant storage system
//! that manages FUSE mounts for multiple user namespaces on a single node.

mod mount_manager;
mod namespace_watcher;

pub use mount_manager::{MountError, MountInfo, MountManager, MountStatus, DEFAULT_BASE_PATH};
pub use namespace_watcher::{NamespaceWatcher, WatcherError};
