use async_trait::async_trait;
use bytes::Bytes;

use crate::{
    config::StorageConfig,
    error::{Result, StorageError},
};

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

/// S3/R2 backend using the AWS SDK
/// Uses static credentials compatible with Cloudflare R2
pub struct S3Backend {
    client: aws_sdk_s3::Client,
    bucket: String,
    prefix: String,
}

impl S3Backend {
    /// Create a new S3/R2 backend from configuration
    pub async fn from_config(config: &StorageConfig) -> Result<Self> {
        let bucket = config
            .bucket
            .as_ref()
            .ok_or_else(|| StorageError::InvalidConfig("bucket is required".to_string()))?
            .clone();

        let credentials = config
            .credentials
            .as_ref()
            .ok_or_else(|| StorageError::InvalidConfig("credentials are required".to_string()))?;

        let client = match config.backend.as_str() {
            "s3" | "r2" => {
                // Get credentials
                let access_key_id = credentials.get("access_key_id").ok_or_else(|| {
                    StorageError::InvalidConfig("access_key_id is required".to_string())
                })?;

                let secret_access_key = credentials.get("secret_access_key").ok_or_else(|| {
                    StorageError::InvalidConfig("secret_access_key is required".to_string())
                })?;

                let region = credentials
                    .get("region")
                    .map(|s| s.as_str())
                    .unwrap_or("auto");

                // Build AWS config with static credentials (no validation)
                let aws_creds = aws_sdk_s3::config::Credentials::new(
                    access_key_id,
                    secret_access_key,
                    None,               // session token not used with R2/S3 static credentials
                    None,               // expiry
                    "basilica-storage", // provider name
                );

                let mut config_builder =
                    aws_config::defaults(aws_config::BehaviorVersion::latest())
                        .credentials_provider(aws_creds)
                        .region(aws_sdk_s3::config::Region::new(region.to_string()));

                // For R2 or custom S3 endpoints, set endpoint URL
                if let Some(endpoint) = credentials.get("endpoint") {
                    config_builder = config_builder.endpoint_url(endpoint);
                }

                let aws_config = config_builder.load().await;
                aws_sdk_s3::Client::new(&aws_config)
            }
            "gcs" => {
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

        Ok(Self {
            client,
            bucket,
            prefix,
        })
    }

    /// Get the full object key with prefix
    fn object_key(&self, key: &str) -> String {
        if self.prefix.is_empty() {
            key.to_string()
        } else {
            format!("{}/{}", self.prefix, key)
        }
    }
}

#[async_trait]
impl StorageBackend for S3Backend {
    async fn put(&self, key: &str, data: Bytes) -> Result<()> {
        let object_key = self.object_key(key);

        self.client
            .put_object()
            .bucket(&self.bucket)
            .key(&object_key)
            .body(data.into())
            .send()
            .await
            .map_err(|e| StorageError::BackendError(format!("Failed to put object: {}", e)))?;

        Ok(())
    }

    async fn get(&self, key: &str) -> Result<Bytes> {
        let object_key = self.object_key(key);

        let resp = self
            .client
            .get_object()
            .bucket(&self.bucket)
            .key(&object_key)
            .send()
            .await
            .map_err(|e| StorageError::BackendError(format!("Failed to get object: {}", e)))?;

        let data = resp.body.collect().await.map_err(|e| {
            StorageError::BackendError(format!("Failed to read object body: {}", e))
        })?;

        Ok(data.into_bytes())
    }

    async fn exists(&self, key: &str) -> Result<bool> {
        let object_key = self.object_key(key);

        match self
            .client
            .head_object()
            .bucket(&self.bucket)
            .key(&object_key)
            .send()
            .await
        {
            Ok(_) => Ok(true),
            Err(e) => {
                // Check if it's a not-found error
                let err_str = format!("{:?}", e);
                if err_str.contains("NotFound") || err_str.contains("404") {
                    Ok(false)
                } else {
                    Err(StorageError::BackendError(format!(
                        "Failed to check object existence: {}",
                        e
                    )))
                }
            }
        }
    }

    async fn delete(&self, key: &str) -> Result<()> {
        let object_key = self.object_key(key);

        self.client
            .delete_object()
            .bucket(&self.bucket)
            .key(&object_key)
            .send()
            .await
            .map_err(|e| StorageError::BackendError(format!("Failed to delete object: {}", e)))?;

        Ok(())
    }

    async fn list(&self, prefix: &str) -> Result<Vec<String>> {
        let object_prefix = self.object_key(prefix);

        let resp = self
            .client
            .list_objects_v2()
            .bucket(&self.bucket)
            .prefix(&object_prefix)
            .send()
            .await
            .map_err(|e| StorageError::BackendError(format!("Failed to list objects: {}", e)))?;

        let keys = resp
            .contents()
            .iter()
            .filter_map(|obj| {
                obj.key().and_then(|key| {
                    // Strip the prefix to return relative keys
                    if !self.prefix.is_empty() {
                        key.strip_prefix(&format!("{}/", self.prefix))
                            .map(|s| s.to_string())
                    } else {
                        Some(key.to_string())
                    }
                })
            })
            .collect();

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
        assert!(creds
            .get("endpoint")
            .unwrap()
            .contains("r2.cloudflarestorage.com"));
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
