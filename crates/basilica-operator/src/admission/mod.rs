//! Admission webhook module for validating pod configurations.
//!
//! Implements ValidatingWebhookConfiguration for storage mount path validation.
//! Ensures pods in user namespaces (u-*) can only mount their own namespace's storage.

mod storage_mount_validator;

pub use storage_mount_validator::{
    validate_pod_storage_mounts, AdmissionError, AdmissionResponse, AdmissionReview,
};

use axum::{extract::State, http::StatusCode, routing::post, Json, Router};
use std::sync::Arc;
use tracing::{debug, error, info, warn};

/// State shared across admission webhook handlers.
#[derive(Clone)]
pub struct AdmissionState {
    /// Base path for FUSE mounts (e.g., /var/lib/basilica/fuse)
    pub fuse_base_path: String,
}

impl Default for AdmissionState {
    fn default() -> Self {
        Self {
            fuse_base_path: "/var/lib/basilica/fuse".to_string(),
        }
    }
}

/// Create the admission webhook router.
pub fn admission_router(state: AdmissionState) -> Router {
    Router::new()
        .route("/validate-pod", post(validate_pod_handler))
        .with_state(Arc::new(state))
}

/// Handler for pod validation requests.
async fn validate_pod_handler(
    State(state): State<Arc<AdmissionState>>,
    Json(review): Json<AdmissionReview>,
) -> (StatusCode, Json<AdmissionReview>) {
    let request = match &review.request {
        Some(req) => req,
        None => {
            error!("Admission review missing request");
            return (
                StatusCode::BAD_REQUEST,
                Json(AdmissionReview::response_error(
                    review.request.as_ref().map(|r| r.uid.as_str()),
                    "Missing request in admission review",
                )),
            );
        }
    };

    let uid = &request.uid;
    let namespace = request.namespace.as_deref().unwrap_or("");
    let name = request.name.as_deref().unwrap_or("<unknown>");

    debug!(
        uid = %uid,
        namespace = %namespace,
        name = %name,
        operation = ?request.operation,
        "Processing admission request"
    );

    // Only validate CREATE and UPDATE operations
    if request.operation != "CREATE" && request.operation != "UPDATE" {
        info!(
            uid = %uid,
            operation = ?request.operation,
            "Skipping validation for non-create/update operation"
        );
        return (
            StatusCode::OK,
            Json(AdmissionReview::response_allowed(uid, None)),
        );
    }

    // Only validate pods in user namespaces (u-*)
    if !namespace.starts_with("u-") {
        debug!(
            uid = %uid,
            namespace = %namespace,
            "Allowing pod in non-user namespace"
        );
        return (
            StatusCode::OK,
            Json(AdmissionReview::response_allowed(uid, None)),
        );
    }

    // Parse the pod object
    let pod = match &request.object {
        Some(obj) => match serde_json::from_value::<k8s_openapi::api::core::v1::Pod>(obj.clone()) {
            Ok(p) => p,
            Err(e) => {
                warn!(uid = %uid, error = %e, "Failed to parse pod object");
                return (
                    StatusCode::OK,
                    Json(AdmissionReview::response_allowed(
                        uid,
                        Some("Failed to parse pod, allowing by default"),
                    )),
                );
            }
        },
        None => {
            warn!(uid = %uid, "No object in admission request");
            return (
                StatusCode::OK,
                Json(AdmissionReview::response_allowed(
                    uid,
                    Some("No object in request, allowing by default"),
                )),
            );
        }
    };

    // Validate storage mounts
    match validate_pod_storage_mounts(&pod, namespace, &state.fuse_base_path) {
        Ok(()) => {
            info!(
                uid = %uid,
                namespace = %namespace,
                name = %name,
                "Pod storage mounts validated successfully"
            );
            (
                StatusCode::OK,
                Json(AdmissionReview::response_allowed(uid, None)),
            )
        }
        Err(e) => {
            warn!(
                uid = %uid,
                namespace = %namespace,
                name = %name,
                error = %e,
                "Pod storage mount validation failed"
            );
            (
                StatusCode::OK,
                Json(AdmissionReview::response_denied(uid, &e.to_string())),
            )
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_state() {
        let state = AdmissionState::default();
        assert_eq!(state.fuse_base_path, "/var/lib/basilica/fuse");
    }
}
