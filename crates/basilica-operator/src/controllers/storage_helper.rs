// Storage helper module for CSI-based storage provisioning
use k8s_openapi::api::core::v1::{
    PersistentVolumeClaim, PersistentVolumeClaimSpec, PersistentVolumeClaimVolumeSource,
    ResourceRequirements, Volume,
};
use k8s_openapi::apimachinery::pkg::api::resource::Quantity;
use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
use std::collections::BTreeMap;
use anyhow::Result;

/// Storage backend types supported by Basilica
#[derive(Debug, Clone, PartialEq)]
pub enum StorageBackend {
    S3,
    Gcs,
    Azure,
    Efs,
    Ebs,
}

impl StorageBackend {
    /// Get the appropriate StorageClass name for this backend
    pub fn storage_class_name(&self) -> &str {
        match self {
            StorageBackend::S3 => "basilica-s3",
            StorageBackend::Gcs => "basilica-gcs",
            StorageBackend::Azure => "basilica-azure",
            StorageBackend::Efs => "basilica-efs",
            StorageBackend::Ebs => "basilica-gp3",
        }
    }

    /// Check if this backend supports multi-pod access
    pub fn supports_read_write_many(&self) -> bool {
        matches!(self, StorageBackend::S3 | StorageBackend::Gcs | StorageBackend::Azure | StorageBackend::Efs)
    }

    /// Parse backend from string
    pub fn from_str(s: &str) -> Result<Self> {
        match s.to_lowercase().as_str() {
            "s3" | "aws" => Ok(StorageBackend::S3),
            "gcs" | "gcp" => Ok(StorageBackend::Gcs),
            "azure" | "azureblob" => Ok(StorageBackend::Azure),
            "efs" => Ok(StorageBackend::Efs),
            "ebs" => Ok(StorageBackend::Ebs),
            _ => Err(anyhow::anyhow!("Unknown storage backend: {}", s))
        }
    }
}

/// Build a PVC spec for the given storage configuration
pub fn build_pvc_spec(
    name: &str,
    namespace: &str,
    backend: StorageBackend,
    size: &str,
    labels: BTreeMap<String, String>,
) -> PersistentVolumeClaim {
    let access_modes = if backend.supports_read_write_many() {
        vec!["ReadWriteMany".to_string()]
    } else {
        vec!["ReadWriteOnce".to_string()]
    };

    PersistentVolumeClaim {
        metadata: ObjectMeta {
            name: Some(format!("{}-pvc", name)),
            namespace: Some(namespace.to_string()),
            labels: Some(labels),
            ..Default::default()
        },
        spec: Some(PersistentVolumeClaimSpec {
            access_modes: Some(access_modes),
            storage_class_name: Some(backend.storage_class_name().to_string()),
            resources: Some(ResourceRequirements {
                requests: Some(BTreeMap::from([
                    ("storage".to_string(), Quantity(size.to_string())),
                ])),
                ..Default::default()
            }),
            ..Default::default()
        }),
        ..Default::default()
    }
}

/// Build a Volume that references a PVC
pub fn build_pvc_volume(name: &str, pvc_name: &str) -> Volume {
    Volume {
        name: name.to_string(),
        persistent_volume_claim: Some(PersistentVolumeClaimVolumeSource {
            claim_name: pvc_name.to_string(),
            read_only: false,
        }),
        ..Default::default()
    }
}

/// Detect storage backend from environment or cluster configuration
pub async fn detect_storage_backend() -> Result<StorageBackend> {
    // Check environment variable first
    if let Ok(backend) = std::env::var("BASILICA_STORAGE_BACKEND") {
        return StorageBackend::from_str(&backend);
    }

    // Try to detect from cloud provider metadata
    // AWS
    if is_running_on_aws().await {
        return Ok(StorageBackend::S3);
    }

    // GCP
    if is_running_on_gcp().await {
        return Ok(StorageBackend::Gcs);
    }

    // Azure
    if is_running_on_azure().await {
        return Ok(StorageBackend::Azure);
    }

    // Default to S3 (most compatible)
    Ok(StorageBackend::S3)
}

async fn is_running_on_aws() -> bool {
    // Check AWS metadata endpoint
    match reqwest::Client::new()
        .get("http://169.254.169.254/latest/meta-data/instance-id")
        .timeout(std::time::Duration::from_millis(500))
        .send()
        .await
    {
        Ok(resp) if resp.status().is_success() => true,
        _ => false,
    }
}

async fn is_running_on_gcp() -> bool {
    // Check GCP metadata endpoint
    match reqwest::Client::new()
        .get("http://metadata.google.internal/computeMetadata/v1/instance/id")
        .header("Metadata-Flavor", "Google")
        .timeout(std::time::Duration::from_millis(500))
        .send()
        .await
    {
        Ok(resp) if resp.status().is_success() => true,
        _ => false,
    }
}

async fn is_running_on_azure() -> bool {
    // Check Azure metadata endpoint
    match reqwest::Client::new()
        .get("http://169.254.169.254/metadata/instance/compute/vmId")
        .header("Metadata", "true")
        .query(&[("api-version", "2021-02-01")])
        .timeout(std::time::Duration::from_millis(500))
        .send()
        .await
    {
        Ok(resp) if resp.status().is_success() => true,
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_storage_backend_parsing() {
        assert_eq!(StorageBackend::from_str("s3").unwrap(), StorageBackend::S3);
        assert_eq!(StorageBackend::from_str("AWS").unwrap(), StorageBackend::S3);
        assert_eq!(StorageBackend::from_str("gcs").unwrap(), StorageBackend::Gcs);
        assert_eq!(StorageBackend::from_str("GCP").unwrap(), StorageBackend::Gcs);
        assert_eq!(StorageBackend::from_str("azure").unwrap(), StorageBackend::Azure);
        assert!(StorageBackend::from_str("unknown").is_err());
    }

    #[test]
    fn test_storage_class_names() {
        assert_eq!(StorageBackend::S3.storage_class_name(), "basilica-s3");
        assert_eq!(StorageBackend::Gcs.storage_class_name(), "basilica-gcs");
        assert_eq!(StorageBackend::Azure.storage_class_name(), "basilica-azure");
        assert_eq!(StorageBackend::Efs.storage_class_name(), "basilica-efs");
        assert_eq!(StorageBackend::Ebs.storage_class_name(), "basilica-gp3");
    }

    #[test]
    fn test_access_mode_support() {
        assert!(StorageBackend::S3.supports_read_write_many());
        assert!(StorageBackend::Gcs.supports_read_write_many());
        assert!(StorageBackend::Azure.supports_read_write_many());
        assert!(StorageBackend::Efs.supports_read_write_many());
        assert!(!StorageBackend::Ebs.supports_read_write_many());
    }

    #[test]
    fn test_build_pvc_spec() {
        let mut labels = BTreeMap::new();
        labels.insert("app".to_string(), "test".to_string());

        let pvc = build_pvc_spec(
            "test-deployment",
            "default",
            StorageBackend::S3,
            "10Gi",
            labels.clone(),
        );

        assert_eq!(pvc.metadata.name, Some("test-deployment-pvc".to_string()));
        assert_eq!(pvc.metadata.namespace, Some("default".to_string()));

        let spec = pvc.spec.unwrap();
        assert_eq!(spec.storage_class_name, Some("basilica-s3".to_string()));
        assert_eq!(spec.access_modes, Some(vec!["ReadWriteMany".to_string()]));

        let requests = spec.resources.unwrap().requests.unwrap();
        assert_eq!(requests.get("storage").unwrap().0, "10Gi");
    }

    #[test]
    fn test_build_pvc_volume() {
        let volume = build_pvc_volume("storage", "my-app-pvc");

        assert_eq!(volume.name, "storage");
        let pvc_source = volume.persistent_volume_claim.unwrap();
        assert_eq!(pvc_source.claim_name, "my-app-pvc");
        assert_eq!(pvc_source.read_only, false);
    }
}