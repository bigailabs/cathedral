use async_trait::async_trait;
use bytes::Bytes;
use object_store::{aws::AmazonS3Builder, path::Path as ObjectPath, ObjectStore};
use std::sync::Arc;

use crate::{config::StorageConfig, error::{Result, StorageError}};

/// Trait for storage backends
#[async_trait]
pub trait StorageBackend: Send + Sync {
    /// Upload data to the storage backend
    async fn put(&self, key: &str, data: Bytes) -> Result<()>;

    /// Download data from the storage backend
    async fn get(&self, key: &str) -> Result<Bytes>;

    /// Check if an object exists
    async fn exists(&self, key: &str) -> Result<bool>;

    /// Delete an object
    async fn delete(&self, key: &str) -> Result<()>;

    /// List objects with a given prefix
    async fn list(&self, prefix: &str) -> Result<Vec<String>>;
}

/// Object storage backend using the `object_store` crate
/// Supports S3, R2, GCS, and other S3-compatible services
pub struct ObjectStoreBackend {
    store: Arc<dyn ObjectStore>,
    prefix: String,
}

impl ObjectStoreBackend {
    /// Create a new object store backend from configuration
    pub fn from_config(config: &StorageConfig) -> Result<Self> {
        let bucket = config
            .bucket
            .as_ref()
            .ok_or_else(|| StorageError::InvalidConfig("bucket is required".to_string()))?;

        let credentials = config
            .credentials
            .as_ref()
            .ok_or_else(|| StorageError::InvalidConfig("credentials are required".to_string()))?;

        let store: Arc<dyn ObjectStore> = match config.backend.as_str() {
            "s3" | "r2" => {
                // Both S3 and R2 use the S3 API
                let mut builder = AmazonS3Builder::new()
                    .with_bucket_name(bucket);

                // Set credentials
                if let Some(access_key) = credentials.get("access_key_id") {
                    builder = builder.with_access_key_id(access_key);
                }
                if let Some(secret_key) = credentials.get("secret_access_key") {
                    builder = builder.with_secret_access_key(secret_key);
                }

                // For R2, set custom endpoint
                if let Some(endpoint) = credentials.get("endpoint") {
                    builder = builder.with_endpoint(endpoint);
                }

                // For S3, set region
                if let Some(region) = credentials.get("region") {
                    builder = builder.with_region(region);
                }

                Arc::new(builder.build()?)
            }
            "gcs" => {
                // GCS support - would use GoogleCloudStorageBuilder
                // For now, return an error as we're focusing on R2/S3
                return Err(StorageError::InvalidConfig(
                    "GCS backend not yet implemented".to_string(),
                ));
            }
            backend => {
                return Err(StorageError::InvalidConfig(format!(
                    "unsupported backend: {}",
                    backend
                )));
            }
        };

        let prefix = config.prefix.clone().unwrap_or_default();

        Ok(Self { store, prefix })
    }

    /// Get the full object path with prefix
    fn object_path(&self, key: &str) -> ObjectPath {
        if self.prefix.is_empty() {
            ObjectPath::from(key)
        } else {
            ObjectPath::from(format!("{}/{}", self.prefix, key))
        }
    }
}

#[async_trait]
impl StorageBackend for ObjectStoreBackend {
    async fn put(&self, key: &str, data: Bytes) -> Result<()> {
        let path = self.object_path(key);
        self.store.put(&path, data.into()).await?;
        Ok(())
    }

    async fn get(&self, key: &str) -> Result<Bytes> {
        let path = self.object_path(key);
        let result = self.store.get(&path).await?;
        let bytes = result.bytes().await?;
        Ok(bytes)
    }

    async fn exists(&self, key: &str) -> Result<bool> {
        let path = self.object_path(key);
        match self.store.head(&path).await {
            Ok(_) => Ok(true),
            Err(object_store::Error::NotFound { .. }) => Ok(false),
            Err(e) => Err(e.into()),
        }
    }

    async fn delete(&self, key: &str) -> Result<()> {
        let path = self.object_path(key);
        self.store.delete(&path).await?;
        Ok(())
    }

    async fn list(&self, prefix: &str) -> Result<Vec<String>> {
        use futures::stream::StreamExt;

        let path = self.object_path(prefix);
        let mut list_stream = self.store.list(Some(&path));

        let mut keys = Vec::new();
        while let Some(meta) = list_stream.next().await {
            let meta = meta?;
            if let Some(key) = meta.location.as_ref().strip_prefix(&self.prefix) {
                keys.push(key.trim_start_matches('/').to_string());
            } else {
                keys.push(meta.location.to_string());
            }
        }

        Ok(keys)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_storage_config_r2() {
        let config = StorageConfig::r2("account123", "key", "secret", "my-bucket")
            .with_prefix("experiments/exp-001");

        assert_eq!(config.backend, "r2");
        assert_eq!(config.bucket, Some("my-bucket".to_string()));
        assert_eq!(config.prefix, Some("experiments/exp-001".to_string()));

        let creds = config.credentials.unwrap();
        assert_eq!(creds.get("access_key_id"), Some(&"key".to_string()));
        assert_eq!(creds.get("secret_access_key"), Some(&"secret".to_string()));
        assert!(creds.get("endpoint").unwrap().contains("r2.cloudflarestorage.com"));
    }

    #[test]
    fn test_storage_config_s3() {
        let config = StorageConfig::s3("us-west-2", "key", "secret", "my-bucket");

        assert_eq!(config.backend, "s3");
        assert_eq!(config.bucket, Some("my-bucket".to_string()));

        let creds = config.credentials.unwrap();
        assert_eq!(creds.get("region"), Some(&"us-west-2".to_string()));
    }
}
