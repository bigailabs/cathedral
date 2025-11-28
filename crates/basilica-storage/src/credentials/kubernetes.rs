//! Kubernetes-based credential provider.
//!
//! Reads storage credentials from Kubernetes secrets in user namespaces.

use super::{CredentialError, CredentialProvider, StorageCredentials};
use async_trait::async_trait;
use k8s_openapi::api::core::v1::Secret;
use kube::api::Api;
use kube::Client;
use std::collections::BTreeMap;

/// Secret names to try in order of preference.
const SECRET_CANDIDATES: &[&str] = &[
    "basilica-r2-credentials",
    "basilica-s3-credentials",
    "basilica-gcs-credentials",
];

/// Default cache size in MB if not specified in secret.
const DEFAULT_CACHE_SIZE_MB: usize = 2048;

/// Credential provider that reads secrets from Kubernetes namespaces.
///
/// Security: Only reads secrets from namespaces starting with "u-".
/// This is enforced at the application level as an additional safety layer
/// on top of Kubernetes RBAC.
pub struct KubernetesCredentialProvider {
    client: Client,
}

impl KubernetesCredentialProvider {
    /// Create a new Kubernetes credential provider.
    ///
    /// Uses in-cluster configuration when running inside Kubernetes,
    /// or kubeconfig when running locally.
    pub async fn new() -> Result<Self, CredentialError> {
        let client = Client::try_default()
            .await
            .map_err(|e| CredentialError::KubernetesError(e.to_string()))?;

        Ok(Self { client })
    }

    /// Create from an existing Kubernetes client.
    pub fn from_client(client: Client) -> Self {
        Self { client }
    }

    /// Validate that the namespace is a user namespace.
    fn validate_namespace(namespace: &str) -> Result<(), CredentialError> {
        if !namespace.starts_with("u-") {
            tracing::error!(
                target: "security_audit",
                event_type = "credential_access_denied",
                severity = "error",
                namespace = %namespace,
                reason = "non_user_namespace",
                "Security violation: attempted to read credentials from non-user namespace"
            );
            return Err(CredentialError::SecurityViolation(format!(
                "Cannot read credentials from non-user namespace '{}'. Only 'u-*' namespaces are allowed.",
                namespace
            )));
        }
        Ok(())
    }

    /// Find and read a storage secret from the namespace.
    async fn find_secret(&self, namespace: &str) -> Result<Secret, CredentialError> {
        let api: Api<Secret> = Api::namespaced(self.client.clone(), namespace);

        // Try user-specific secret first
        let username = namespace.strip_prefix("u-").unwrap_or(namespace);
        let user_secret_name = format!("user-storage-{}", username);

        if let Ok(secret) = api.get(&user_secret_name).await {
            tracing::debug!(
                namespace = %namespace,
                secret_name = %user_secret_name,
                "Found user-specific storage secret"
            );
            return Ok(secret);
        }

        // Fall back to standard secret names
        for secret_name in SECRET_CANDIDATES {
            if let Ok(secret) = api.get(secret_name).await {
                tracing::debug!(
                    namespace = %namespace,
                    secret_name = %secret_name,
                    "Found storage secret"
                );
                return Ok(secret);
            }
        }

        Err(CredentialError::SecretNotFound(format!(
            "No valid storage secret found in namespace '{}'. Tried: user-storage-{}, {:?}",
            namespace, username, SECRET_CANDIDATES
        )))
    }

    /// Extract string data from secret.
    fn extract_secret_data(secret: &Secret) -> Result<BTreeMap<String, String>, CredentialError> {
        let data = secret
            .data
            .as_ref()
            .ok_or_else(|| CredentialError::InvalidData("Secret has no data".to_string()))?;

        let mut result = BTreeMap::new();
        for (key, value) in data {
            let decoded = String::from_utf8(value.0.clone()).map_err(|e| {
                CredentialError::InvalidData(format!("Failed to decode key '{}': {}", key, e))
            })?;
            result.insert(key.clone(), decoded);
        }

        Ok(result)
    }

    /// Get a required field from secret data.
    fn get_required_field(
        data: &BTreeMap<String, String>,
        primary_key: &str,
        fallback_key: &str,
    ) -> Result<String, CredentialError> {
        data.get(primary_key)
            .or_else(|| data.get(fallback_key))
            .cloned()
            .ok_or_else(|| {
                CredentialError::MissingField(format!(
                    "Missing '{}' or '{}' in secret",
                    primary_key, fallback_key
                ))
            })
    }
}

#[async_trait]
impl CredentialProvider for KubernetesCredentialProvider {
    async fn get_credentials(
        &self,
        namespace: &str,
    ) -> Result<StorageCredentials, CredentialError> {
        Self::validate_namespace(namespace)?;

        tracing::info!(
            namespace = %namespace,
            "Reading storage credentials from namespace secret"
        );

        let secret = self.find_secret(namespace).await?;
        let secret_name = secret.metadata.name.as_deref().unwrap_or("unknown");

        let data = Self::extract_secret_data(&secret)?;

        let access_key_id =
            Self::get_required_field(&data, "STORAGE_ACCESS_KEY_ID", "access_key_id")?;
        let secret_access_key =
            Self::get_required_field(&data, "STORAGE_SECRET_ACCESS_KEY", "secret_access_key")?;
        let endpoint = Self::get_required_field(&data, "STORAGE_ENDPOINT", "endpoint")?;
        let bucket = Self::get_required_field(&data, "STORAGE_BUCKET", "bucket")?;

        let region = data
            .get("STORAGE_REGION")
            .or_else(|| data.get("region"))
            .cloned();

        let cache_size_mb = data
            .get("cache_size_mb")
            .and_then(|s| s.parse::<usize>().ok())
            .unwrap_or(DEFAULT_CACHE_SIZE_MB);

        tracing::info!(
            target: "security_audit",
            event_type = "credential_access_granted",
            severity = "info",
            namespace = %namespace,
            secret_name = %secret_name,
            bucket = %bucket,
            "Successfully retrieved storage credentials from namespace"
        );

        Ok(StorageCredentials {
            access_key_id,
            secret_access_key,
            endpoint,
            bucket,
            region,
            cache_size_mb,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_validate_namespace_valid_user_namespace() {
        assert!(KubernetesCredentialProvider::validate_namespace("u-alice").is_ok());
        assert!(KubernetesCredentialProvider::validate_namespace("u-bob-123").is_ok());
        assert!(KubernetesCredentialProvider::validate_namespace("u-github-434149").is_ok());
    }

    #[test]
    fn test_validate_namespace_rejects_non_user_namespace() {
        let result = KubernetesCredentialProvider::validate_namespace("default");
        assert!(result.is_err());
        assert!(matches!(result, Err(CredentialError::SecurityViolation(_))));
    }

    #[test]
    fn test_validate_namespace_rejects_system_namespace() {
        let result = KubernetesCredentialProvider::validate_namespace("basilica-system");
        assert!(result.is_err());
        assert!(matches!(result, Err(CredentialError::SecurityViolation(_))));
    }

    #[test]
    fn test_validate_namespace_rejects_kube_system() {
        let result = KubernetesCredentialProvider::validate_namespace("kube-system");
        assert!(result.is_err());
        assert!(matches!(result, Err(CredentialError::SecurityViolation(_))));
    }

    #[test]
    fn test_validate_namespace_rejects_storage_namespace() {
        let result = KubernetesCredentialProvider::validate_namespace("basilica-storage");
        assert!(result.is_err());
        assert!(matches!(result, Err(CredentialError::SecurityViolation(_))));
    }

    #[test]
    fn test_get_required_field_primary_key() {
        let mut data = BTreeMap::new();
        data.insert("STORAGE_BUCKET".to_string(), "my-bucket".to_string());

        let result =
            KubernetesCredentialProvider::get_required_field(&data, "STORAGE_BUCKET", "bucket");
        assert_eq!(result.unwrap(), "my-bucket");
    }

    #[test]
    fn test_get_required_field_fallback_key() {
        let mut data = BTreeMap::new();
        data.insert("bucket".to_string(), "fallback-bucket".to_string());

        let result =
            KubernetesCredentialProvider::get_required_field(&data, "STORAGE_BUCKET", "bucket");
        assert_eq!(result.unwrap(), "fallback-bucket");
    }

    #[test]
    fn test_get_required_field_missing() {
        let data = BTreeMap::new();

        let result =
            KubernetesCredentialProvider::get_required_field(&data, "STORAGE_BUCKET", "bucket");
        assert!(result.is_err());
        assert!(matches!(result, Err(CredentialError::MissingField(_))));
    }

    #[test]
    fn test_extract_secret_data_empty() {
        let secret = Secret {
            data: None,
            ..Default::default()
        };

        let result = KubernetesCredentialProvider::extract_secret_data(&secret);
        assert!(result.is_err());
        assert!(matches!(result, Err(CredentialError::InvalidData(_))));
    }

    #[test]
    fn test_extract_secret_data_valid() {
        use k8s_openapi::ByteString;

        let mut data = BTreeMap::new();
        data.insert(
            "STORAGE_BUCKET".to_string(),
            ByteString("test-bucket".as_bytes().to_vec()),
        );

        let secret = Secret {
            data: Some(data),
            ..Default::default()
        };

        let result = KubernetesCredentialProvider::extract_secret_data(&secret).unwrap();
        assert_eq!(result.get("STORAGE_BUCKET").unwrap(), "test-bucket");
    }
}
