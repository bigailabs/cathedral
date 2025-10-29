use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// Storage configuration for object storage backends
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct StorageConfig {
    /// Backend type: "s3", "r2", "gcs", etc.
    pub backend: String,

    /// Bucket name
    pub bucket: Option<String>,

    /// Object key prefix
    pub prefix: Option<String>,

    /// Backend-specific credentials
    pub credentials: Option<HashMap<String, String>>,
}

impl StorageConfig {
    /// Create a new R2 storage configuration
    pub fn r2(account_id: &str, access_key: &str, secret_key: &str, bucket: &str) -> Self {
        let mut credentials = HashMap::new();
        credentials.insert("access_key_id".to_string(), access_key.to_string());
        credentials.insert("secret_access_key".to_string(), secret_key.to_string());
        // R2 requires us-east-1 as the region for S3 API compatibility
        credentials.insert("region".to_string(), "us-east-1".to_string());
        credentials.insert(
            "endpoint".to_string(),
            format!("https://{}.r2.cloudflarestorage.com", account_id),
        );

        Self {
            backend: "r2".to_string(),
            bucket: Some(bucket.to_string()),
            prefix: None,
            credentials: Some(credentials),
        }
    }

    /// Create a new S3 storage configuration
    pub fn s3(region: &str, access_key: &str, secret_key: &str, bucket: &str) -> Self {
        let mut credentials = HashMap::new();
        credentials.insert("access_key_id".to_string(), access_key.to_string());
        credentials.insert("secret_access_key".to_string(), secret_key.to_string());
        credentials.insert("region".to_string(), region.to_string());

        Self {
            backend: "s3".to_string(),
            bucket: Some(bucket.to_string()),
            prefix: None,
            credentials: Some(credentials),
        }
    }

    /// Create a new GCS storage configuration
    pub fn gcs(service_account_key: &str, bucket: &str) -> Self {
        let mut credentials = HashMap::new();
        credentials.insert("service_account_key".to_string(), service_account_key.to_string());

        Self {
            backend: "gcs".to_string(),
            bucket: Some(bucket.to_string()),
            prefix: None,
            credentials: Some(credentials),
        }
    }

    /// Set the object key prefix
    pub fn with_prefix(mut self, prefix: &str) -> Self {
        self.prefix = Some(prefix.to_string());
        self
    }
}
