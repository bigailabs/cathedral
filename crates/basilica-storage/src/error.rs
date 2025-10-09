use thiserror::Error;

pub type Result<T> = std::result::Result<T, StorageError>;

#[derive(Error, Debug)]
pub enum StorageError {
    #[error("Object store error: {0}")]
    ObjectStore(#[from] object_store::Error),

    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),

    #[error("Serialization error: {0}")]
    Serialization(#[from] serde_json::Error),

    #[error("Invalid configuration: {0}")]
    InvalidConfig(String),

    #[error("Snapshot not found: {0}")]
    SnapshotNotFound(String),

    #[error("Invalid path: {0}")]
    InvalidPath(String),
}
