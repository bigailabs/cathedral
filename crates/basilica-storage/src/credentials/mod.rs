//! Credential providers for storage backends.
//!
//! This module provides abstractions for retrieving storage credentials
//! from various sources. The primary implementation is [`KubernetesCredentialProvider`]
//! which reads secrets from Kubernetes namespaces.

mod kubernetes;

pub use kubernetes::KubernetesCredentialProvider;

use async_trait::async_trait;

/// Storage credentials for object storage backends.
#[derive(Debug, Clone)]
pub struct StorageCredentials {
    pub access_key_id: String,
    pub secret_access_key: String,
    pub endpoint: String,
    pub bucket: String,
    pub region: Option<String>,
    pub cache_size_mb: usize,
}

/// Error type for credential provider operations.
#[derive(Debug, thiserror::Error)]
pub enum CredentialError {
    #[error("Security violation: {0}")]
    SecurityViolation(String),

    #[error("Secret not found: {0}")]
    SecretNotFound(String),

    #[error("Missing required field: {0}")]
    MissingField(String),

    #[error("Invalid secret data: {0}")]
    InvalidData(String),

    #[error("Kubernetes API error: {0}")]
    KubernetesError(String),
}

/// Trait for credential providers.
///
/// Implementations retrieve storage credentials from various sources
/// (Kubernetes secrets, Vault, environment variables, etc.).
#[async_trait]
pub trait CredentialProvider: Send + Sync {
    /// Retrieve storage credentials for the given namespace.
    ///
    /// # Arguments
    /// * `namespace` - The Kubernetes namespace to retrieve credentials for.
    ///                 Must start with "u-" for user namespaces.
    ///
    /// # Errors
    /// Returns [`CredentialError::SecurityViolation`] if namespace doesn't start with "u-".
    /// Returns [`CredentialError::SecretNotFound`] if no valid secret exists.
    async fn get_credentials(&self, namespace: &str)
        -> Result<StorageCredentials, CredentialError>;
}
