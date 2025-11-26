//! Storage mount path validator for admission webhook.
//!
//! Validates that pods in user namespaces (u-*) only mount storage
//! from their own namespace's directory under the FUSE base path.

use k8s_openapi::api::core::v1::Pod;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// Admission webhook error types.
#[derive(Debug, thiserror::Error)]
pub enum AdmissionError {
    #[error("Cross-namespace mount attempt: pod in namespace '{pod_namespace}' tried to mount '{mount_path}' which belongs to namespace '{target_namespace}'")]
    CrossNamespaceMount {
        pod_namespace: String,
        mount_path: String,
        target_namespace: String,
    },

    #[error(
        "Invalid mount path: '{path}' is under FUSE base path but doesn't match expected format"
    )]
    InvalidMountFormat { path: String },

    #[error("Direct FUSE base path mount not allowed: '{path}'")]
    DirectBaseMountNotAllowed { path: String },
}

/// Kubernetes admission review request/response wrapper.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AdmissionReview {
    pub api_version: String,
    pub kind: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub request: Option<AdmissionRequest>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub response: Option<AdmissionResponse>,
}

/// Admission request from Kubernetes API server.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AdmissionRequest {
    pub uid: String,
    pub kind: GroupVersionKind,
    pub resource: GroupVersionResource,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sub_resource: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub request_kind: Option<GroupVersionKind>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub request_resource: Option<GroupVersionResource>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub namespace: Option<String>,
    pub operation: String,
    pub user_info: UserInfo,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub object: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub old_object: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dry_run: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub options: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct GroupVersionKind {
    pub group: String,
    pub version: String,
    pub kind: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct GroupVersionResource {
    pub group: String,
    pub version: String,
    pub resource: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UserInfo {
    pub username: String,
    #[serde(default)]
    pub groups: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub uid: Option<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub extra: BTreeMap<String, Vec<String>>,
}

/// Admission response to Kubernetes API server.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AdmissionResponse {
    pub uid: String,
    pub allowed: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub status: Option<Status>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub warnings: Option<Vec<String>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Status {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub code: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

impl AdmissionReview {
    /// Create an allowed response.
    pub fn response_allowed(uid: &str, warning: Option<&str>) -> Self {
        Self {
            api_version: "admission.k8s.io/v1".to_string(),
            kind: "AdmissionReview".to_string(),
            request: None,
            response: Some(AdmissionResponse {
                uid: uid.to_string(),
                allowed: true,
                status: None,
                warnings: warning.map(|w| vec![w.to_string()]),
            }),
        }
    }

    /// Create a denied response.
    pub fn response_denied(uid: &str, message: &str) -> Self {
        Self {
            api_version: "admission.k8s.io/v1".to_string(),
            kind: "AdmissionReview".to_string(),
            request: None,
            response: Some(AdmissionResponse {
                uid: uid.to_string(),
                allowed: false,
                status: Some(Status {
                    code: Some(403),
                    message: Some(message.to_string()),
                    reason: Some("Forbidden".to_string()),
                }),
                warnings: None,
            }),
        }
    }

    /// Create an error response.
    pub fn response_error(uid: Option<&str>, message: &str) -> Self {
        Self {
            api_version: "admission.k8s.io/v1".to_string(),
            kind: "AdmissionReview".to_string(),
            request: None,
            response: Some(AdmissionResponse {
                uid: uid.unwrap_or("").to_string(),
                allowed: false,
                status: Some(Status {
                    code: Some(400),
                    message: Some(message.to_string()),
                    reason: Some("BadRequest".to_string()),
                }),
                warnings: None,
            }),
        }
    }
}

/// Validate pod storage mounts for cross-namespace access.
///
/// Ensures that pods in user namespaces (u-*) only mount storage
/// from their own namespace's directory under the FUSE base path.
pub fn validate_pod_storage_mounts(
    pod: &Pod,
    pod_namespace: &str,
    fuse_base_path: &str,
) -> Result<(), AdmissionError> {
    let spec = match &pod.spec {
        Some(s) => s,
        None => return Ok(()), // No spec, nothing to validate
    };

    let volumes = match &spec.volumes {
        Some(v) => v,
        None => return Ok(()), // No volumes, nothing to validate
    };

    let fuse_base = normalize_path(fuse_base_path);

    for volume in volumes {
        let host_path = match &volume.host_path {
            Some(hp) => hp,
            None => continue, // Not a hostPath volume
        };

        let volume_path = normalize_path(&host_path.path);

        // Check if this is a FUSE mount path
        if !volume_path.starts_with(&fuse_base) {
            continue; // Not a FUSE mount, skip
        }

        // Don't allow mounting the base path directly
        if volume_path == fuse_base {
            return Err(AdmissionError::DirectBaseMountNotAllowed {
                path: host_path.path.clone(),
            });
        }

        // Extract namespace from path: /var/lib/basilica/fuse/{namespace}/...
        let remainder = &volume_path[fuse_base.len()..];
        let remainder = remainder.trim_start_matches('/');

        let target_namespace = match remainder.split('/').next() {
            Some(ns) if !ns.is_empty() => ns,
            _ => {
                return Err(AdmissionError::InvalidMountFormat {
                    path: host_path.path.clone(),
                })
            }
        };

        // Validate namespace matches
        if target_namespace != pod_namespace {
            return Err(AdmissionError::CrossNamespaceMount {
                pod_namespace: pod_namespace.to_string(),
                mount_path: host_path.path.clone(),
                target_namespace: target_namespace.to_string(),
            });
        }
    }

    Ok(())
}

/// Normalize path by removing trailing slashes.
fn normalize_path(path: &str) -> String {
    path.trim_end_matches('/').to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use k8s_openapi::api::core::v1::{HostPathVolumeSource, PodSpec, Volume};

    fn create_pod_with_hostpath(path: &str) -> Pod {
        Pod {
            spec: Some(PodSpec {
                volumes: Some(vec![Volume {
                    name: "test-volume".to_string(),
                    host_path: Some(HostPathVolumeSource {
                        path: path.to_string(),
                        type_: Some("Directory".to_string()),
                    }),
                    ..Default::default()
                }]),
                containers: vec![],
                ..Default::default()
            }),
            ..Default::default()
        }
    }

    #[test]
    fn test_valid_same_namespace_mount() {
        let pod = create_pod_with_hostpath("/var/lib/basilica/fuse/u-alice");
        let result = validate_pod_storage_mounts(&pod, "u-alice", "/var/lib/basilica/fuse");
        assert!(result.is_ok());
    }

    #[test]
    fn test_valid_same_namespace_mount_with_subpath() {
        let pod = create_pod_with_hostpath("/var/lib/basilica/fuse/u-alice/data");
        let result = validate_pod_storage_mounts(&pod, "u-alice", "/var/lib/basilica/fuse");
        assert!(result.is_ok());
    }

    #[test]
    fn test_cross_namespace_mount_rejected() {
        let pod = create_pod_with_hostpath("/var/lib/basilica/fuse/u-bob");
        let result = validate_pod_storage_mounts(&pod, "u-alice", "/var/lib/basilica/fuse");

        assert!(result.is_err());
        match result.unwrap_err() {
            AdmissionError::CrossNamespaceMount {
                pod_namespace,
                target_namespace,
                ..
            } => {
                assert_eq!(pod_namespace, "u-alice");
                assert_eq!(target_namespace, "u-bob");
            }
            e => panic!("Expected CrossNamespaceMount error, got: {:?}", e),
        }
    }

    #[test]
    fn test_direct_base_path_mount_rejected() {
        let pod = create_pod_with_hostpath("/var/lib/basilica/fuse");
        let result = validate_pod_storage_mounts(&pod, "u-alice", "/var/lib/basilica/fuse");

        assert!(result.is_err());
        assert!(matches!(
            result.unwrap_err(),
            AdmissionError::DirectBaseMountNotAllowed { .. }
        ));
    }

    #[test]
    fn test_non_fuse_hostpath_allowed() {
        let pod = create_pod_with_hostpath("/var/log/app");
        let result = validate_pod_storage_mounts(&pod, "u-alice", "/var/lib/basilica/fuse");
        assert!(result.is_ok());
    }

    #[test]
    fn test_pod_without_volumes_allowed() {
        let pod = Pod {
            spec: Some(PodSpec {
                containers: vec![],
                ..Default::default()
            }),
            ..Default::default()
        };
        let result = validate_pod_storage_mounts(&pod, "u-alice", "/var/lib/basilica/fuse");
        assert!(result.is_ok());
    }

    #[test]
    fn test_pod_without_spec_allowed() {
        let pod = Pod::default();
        let result = validate_pod_storage_mounts(&pod, "u-alice", "/var/lib/basilica/fuse");
        assert!(result.is_ok());
    }

    #[test]
    fn test_normalize_path_trailing_slash() {
        assert_eq!(
            normalize_path("/var/lib/basilica/fuse/"),
            "/var/lib/basilica/fuse"
        );
        assert_eq!(
            normalize_path("/var/lib/basilica/fuse"),
            "/var/lib/basilica/fuse"
        );
    }

    #[test]
    fn test_admission_review_allowed() {
        let review = AdmissionReview::response_allowed("test-uid", None);
        assert_eq!(review.response.as_ref().unwrap().uid, "test-uid");
        assert!(review.response.as_ref().unwrap().allowed);
    }

    #[test]
    fn test_admission_review_denied() {
        let review = AdmissionReview::response_denied("test-uid", "Test denial");
        assert_eq!(review.response.as_ref().unwrap().uid, "test-uid");
        assert!(!review.response.as_ref().unwrap().allowed);
        assert_eq!(
            review
                .response
                .as_ref()
                .unwrap()
                .status
                .as_ref()
                .unwrap()
                .message
                .as_ref()
                .unwrap(),
            "Test denial"
        );
    }

    #[test]
    fn test_admission_review_with_warning() {
        let review = AdmissionReview::response_allowed("test-uid", Some("Test warning"));
        assert!(review.response.as_ref().unwrap().allowed);
        assert_eq!(
            review.response.as_ref().unwrap().warnings.as_ref().unwrap()[0],
            "Test warning"
        );
    }
}
