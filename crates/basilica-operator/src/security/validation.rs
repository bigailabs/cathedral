use anyhow::{bail, Result};

use crate::crd::user_deployment::{PersistentStorageSpec, StorageSpec};

const SYSTEM_CREDENTIAL_SECRETS: &[&str] = &[
    "basilica-r2-credentials",
    "basilica-s3-credentials",
    "basilica-gcs-credentials",
];

const ALLOWED_CREDENTIAL_SECRET_PREFIXES: &[&str] = &[
    "basilica-r2-credentials",
    "basilica-s3-credentials",
    "basilica-gcs-credentials",
    "user-storage-",
];

fn is_system_credentials(secret_name: &str) -> bool {
    SYSTEM_CREDENTIAL_SECRETS.contains(&secret_name)
}

const MIN_CACHE_SIZE_MB: usize = 512;
const MAX_CACHE_SIZE_MB: usize = 16384;

const MIN_SYNC_INTERVAL_MS: u64 = 1000;
const MAX_SYNC_INTERVAL_MS: u64 = 3600000;

pub fn validate_storage_spec(
    namespace: &str,
    deployment_name: &str,
    storage_spec: &Option<StorageSpec>,
) -> Result<()> {
    let storage = match storage_spec {
        Some(s) => s,
        None => return Ok(()),
    };

    let persistent = match &storage.persistent {
        Some(p) => p,
        None => return Ok(()),
    };

    validate_credentials_secret(namespace, deployment_name, persistent)?;
    validate_bucket_name(namespace, deployment_name, persistent)?;
    validate_cache_size(namespace, deployment_name, persistent)?;
    validate_sync_interval(namespace, deployment_name, persistent)?;

    Ok(())
}

fn validate_credentials_secret(
    namespace: &str,
    deployment_name: &str,
    persistent: &PersistentStorageSpec,
) -> Result<()> {
    if let Some(ref creds_secret) = persistent.credentials_secret {
        let is_allowed = ALLOWED_CREDENTIAL_SECRET_PREFIXES
            .iter()
            .any(|prefix| creds_secret.starts_with(prefix));

        if !is_allowed {
            tracing::error!(
                target: "security_audit",
                event_type = "storage_validation_failed",
                severity = "error",
                namespace = %namespace,
                deployment = %deployment_name,
                credentials_secret = %creds_secret,
                reason = "unauthorized_credentials_secret",
                "Storage validation failed: unauthorized credentials secret"
            );

            bail!(
                "Invalid credentials_secret '{}' for deployment '{}' in namespace '{}'. \
                 Allowed prefixes: {:?}",
                creds_secret,
                deployment_name,
                namespace,
                ALLOWED_CREDENTIAL_SECRET_PREFIXES
            );
        }

        tracing::debug!(
            namespace = %namespace,
            deployment = %deployment_name,
            credentials_secret = %creds_secret,
            "Credentials secret validation passed"
        );
    }

    Ok(())
}

fn validate_bucket_name(
    namespace: &str,
    deployment_name: &str,
    persistent: &PersistentStorageSpec,
) -> Result<()> {
    let uses_system_creds = persistent
        .credentials_secret
        .as_ref()
        .map(|s| is_system_credentials(s))
        .unwrap_or(true);

    if uses_system_creds && !persistent.bucket.is_empty() {
        tracing::error!(
            target: "security_audit",
            event_type = "storage_validation_failed",
            severity = "error",
            namespace = %namespace,
            deployment = %deployment_name,
            bucket = %persistent.bucket,
            reason = "custom_bucket_with_system_credentials",
            "Storage validation failed: cannot specify custom bucket when using system credentials"
        );

        bail!(
            "Cannot specify custom bucket '{}' for deployment '{}' in namespace '{}'. \
             When using system credentials, bucket is determined by the credentials secret. \
             To use a custom bucket, provide your own credentials via 'user-storage-*' secret.",
            persistent.bucket,
            deployment_name,
            namespace
        );
    }

    Ok(())
}

fn validate_cache_size(
    namespace: &str,
    deployment_name: &str,
    persistent: &PersistentStorageSpec,
) -> Result<()> {
    let cache_size = persistent.cache_size_mb;

    if !(MIN_CACHE_SIZE_MB..=MAX_CACHE_SIZE_MB).contains(&cache_size) {
        tracing::error!(
            target: "security_audit",
            event_type = "storage_validation_failed",
            severity = "error",
            namespace = %namespace,
            deployment = %deployment_name,
            cache_size_mb = cache_size,
            reason = "cache_size_out_of_bounds",
            min_allowed = MIN_CACHE_SIZE_MB,
            max_allowed = MAX_CACHE_SIZE_MB,
            "Storage validation failed: cache size out of bounds"
        );

        bail!(
            "Invalid cache_size_mb {} for deployment '{}' in namespace '{}'. \
             Must be between {} and {} MB",
            cache_size,
            deployment_name,
            namespace,
            MIN_CACHE_SIZE_MB,
            MAX_CACHE_SIZE_MB
        );
    }

    Ok(())
}

fn validate_sync_interval(
    namespace: &str,
    deployment_name: &str,
    persistent: &PersistentStorageSpec,
) -> Result<()> {
    let sync_interval = persistent.sync_interval_ms;

    if !(MIN_SYNC_INTERVAL_MS..=MAX_SYNC_INTERVAL_MS).contains(&sync_interval) {
        tracing::error!(
            target: "security_audit",
            event_type = "storage_validation_failed",
            severity = "error",
            namespace = %namespace,
            deployment = %deployment_name,
            sync_interval_ms = sync_interval,
            reason = "sync_interval_out_of_bounds",
            min_allowed_ms = MIN_SYNC_INTERVAL_MS,
            max_allowed_ms = MAX_SYNC_INTERVAL_MS,
            "Storage validation failed: sync interval out of bounds"
        );

        bail!(
            "Invalid sync_interval_ms {} for deployment '{}' in namespace '{}'. \
             Must be between {} and {} ms",
            sync_interval,
            deployment_name,
            namespace,
            MIN_SYNC_INTERVAL_MS,
            MAX_SYNC_INTERVAL_MS
        );
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::crd::user_deployment::StorageBackend;

    fn create_test_persistent_storage() -> PersistentStorageSpec {
        PersistentStorageSpec {
            enabled: true,
            backend: StorageBackend::R2,
            bucket: String::new(),
            region: None,
            endpoint: Some("https://example.r2.cloudflarestorage.com".to_string()),
            credentials_secret: Some("basilica-r2-credentials".to_string()),
            sync_interval_ms: 1000,
            cache_size_mb: 1024,
            mount_path: "/data".to_string(),
        }
    }

    #[test]
    fn test_validate_system_credentials_without_bucket() {
        let mut persistent = create_test_persistent_storage();
        persistent.credentials_secret = Some("basilica-r2-credentials".to_string());
        persistent.bucket = String::new();

        let storage = Some(StorageSpec {
            ephemeral: None,
            persistent: Some(persistent),
        });

        assert!(validate_storage_spec("u-test", "test-deployment", &storage).is_ok());
    }

    #[test]
    fn test_reject_system_credentials_with_custom_bucket() {
        let mut persistent = create_test_persistent_storage();
        persistent.credentials_secret = Some("basilica-r2-credentials".to_string());
        persistent.bucket = "custom-bucket".to_string();

        let storage = Some(StorageSpec {
            ephemeral: None,
            persistent: Some(persistent),
        });

        let result = validate_storage_spec("u-test", "test-deployment", &storage);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("custom bucket"));
    }

    #[test]
    fn test_validate_user_storage_with_custom_bucket() {
        let mut persistent = create_test_persistent_storage();
        persistent.credentials_secret = Some("user-storage-custom".to_string());
        persistent.bucket = "my-custom-bucket".to_string();

        let storage = Some(StorageSpec {
            ephemeral: None,
            persistent: Some(persistent),
        });

        assert!(validate_storage_spec("u-test", "test-deployment", &storage).is_ok());
    }

    #[test]
    fn test_reject_unauthorized_credentials_secret() {
        let mut persistent = create_test_persistent_storage();
        persistent.credentials_secret = Some("admin-secrets".to_string());

        let storage = Some(StorageSpec {
            ephemeral: None,
            persistent: Some(persistent),
        });

        let result = validate_storage_spec("u-test", "test-deployment", &storage);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("admin-secrets"));
    }

    #[test]
    fn test_reject_cache_size_too_small() {
        let mut persistent = create_test_persistent_storage();
        persistent.cache_size_mb = 256;

        let storage = Some(StorageSpec {
            ephemeral: None,
            persistent: Some(persistent),
        });

        let result = validate_storage_spec("u-test", "test-deployment", &storage);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("cache_size_mb"));
    }

    #[test]
    fn test_reject_cache_size_too_large() {
        let mut persistent = create_test_persistent_storage();
        persistent.cache_size_mb = 20000;

        let storage = Some(StorageSpec {
            ephemeral: None,
            persistent: Some(persistent),
        });

        let result = validate_storage_spec("u-test", "test-deployment", &storage);
        assert!(result.is_err());
    }

    #[test]
    fn test_reject_sync_interval_too_small() {
        let mut persistent = create_test_persistent_storage();
        persistent.sync_interval_ms = 500;

        let storage = Some(StorageSpec {
            ephemeral: None,
            persistent: Some(persistent),
        });

        let result = validate_storage_spec("u-test", "test-deployment", &storage);
        assert!(result.is_err());
    }

    #[test]
    fn test_reject_sync_interval_too_large() {
        let mut persistent = create_test_persistent_storage();
        persistent.sync_interval_ms = 4000000;

        let storage = Some(StorageSpec {
            ephemeral: None,
            persistent: Some(persistent),
        });

        let result = validate_storage_spec("u-test", "test-deployment", &storage);
        assert!(result.is_err());
    }

    #[test]
    fn test_validate_none_storage_spec() {
        assert!(validate_storage_spec("u-test", "test-deployment", &None).is_ok());
    }

    #[test]
    fn test_validate_none_persistent_storage() {
        let storage = Some(StorageSpec {
            ephemeral: None,
            persistent: None,
        });

        assert!(validate_storage_spec("u-test", "test-deployment", &storage).is_ok());
    }
}
