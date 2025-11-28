//! Kubernetes namespace watcher for automatic mount management.
//!
//! Watches for user namespace (u-*) events and triggers mount/unmount operations.

use crate::credentials::CredentialProvider;
use crate::daemon::MountManager;
use futures::StreamExt;
use k8s_openapi::api::core::v1::Namespace;
use kube::runtime::watcher::{watcher, Config, Event};
use kube::{Api, Client};
use std::sync::Arc;
use tokio::sync::watch;
use tokio_util::sync::CancellationToken;

/// Error type for namespace watcher operations.
#[derive(Debug, thiserror::Error)]
pub enum WatcherError {
    #[error("Failed to create Kubernetes client: {0}")]
    ClientError(String),

    #[error("Watcher stream error: {0}")]
    StreamError(String),
}

/// Watches Kubernetes namespaces and manages FUSE mounts accordingly.
///
/// When a user namespace (u-*) is created, triggers a mount operation.
/// When a user namespace is deleted, triggers an unmount operation.
pub struct NamespaceWatcher<P: CredentialProvider + 'static> {
    mount_manager: Arc<MountManager<P>>,
    cancel_token: CancellationToken,
    ready_tx: watch::Sender<bool>,
}

impl<P: CredentialProvider + 'static> NamespaceWatcher<P> {
    /// Create a new namespace watcher.
    pub fn new(mount_manager: Arc<MountManager<P>>) -> (Self, watch::Receiver<bool>) {
        let (ready_tx, ready_rx) = watch::channel(false);
        let watcher = Self {
            mount_manager,
            cancel_token: CancellationToken::new(),
            ready_tx,
        };
        (watcher, ready_rx)
    }

    /// Start watching namespaces.
    ///
    /// This runs until the cancellation token is triggered or an unrecoverable error occurs.
    pub async fn run(&self) -> Result<(), WatcherError> {
        let client = Client::try_default()
            .await
            .map_err(|e| WatcherError::ClientError(e.to_string()))?;

        let api: Api<Namespace> = Api::all(client);

        let config = Config::default();
        let mut stream = watcher(api, config).boxed();

        tracing::info!("Namespace watcher started, watching for u-* namespaces");

        // Mark as ready
        let _ = self.ready_tx.send(true);

        loop {
            tokio::select! {
                _ = self.cancel_token.cancelled() => {
                    tracing::info!("Namespace watcher cancelled");
                    break;
                }
                event = stream.next() => {
                    match event {
                        Some(Ok(event)) => {
                            self.handle_event(event).await;
                        }
                        Some(Err(e)) => {
                            tracing::warn!(error = %e, "Watcher stream error, will reconnect");
                            // The watcher will automatically reconnect on transient errors
                        }
                        None => {
                            tracing::warn!("Watcher stream ended unexpectedly");
                            break;
                        }
                    }
                }
            }
        }

        Ok(())
    }

    /// Handle a namespace event.
    async fn handle_event(&self, event: Event<Namespace>) {
        match event {
            Event::Applied(ns) => {
                if let Some(name) = ns.metadata.name.as_ref() {
                    if is_user_namespace(name) {
                        self.handle_namespace_created(name).await;
                    }
                }
            }
            Event::Deleted(ns) => {
                if let Some(name) = ns.metadata.name.as_ref() {
                    if is_user_namespace(name) {
                        self.handle_namespace_deleted(name).await;
                    }
                }
            }
            Event::Restarted(namespaces) => {
                tracing::debug!(
                    "Watcher restarted, processing {} namespaces",
                    namespaces.len()
                );
                for ns in namespaces {
                    if let Some(name) = ns.metadata.name.as_ref() {
                        if is_user_namespace(name) {
                            self.handle_namespace_created(name).await;
                        }
                    }
                }
            }
        }
    }

    /// Handle namespace creation - create mount if not exists.
    async fn handle_namespace_created(&self, namespace: &str) {
        tracing::info!(namespace = %namespace, "User namespace detected, checking mount");

        // Check if mount already exists
        if self
            .mount_manager
            .get_mount_status(namespace)
            .await
            .is_some()
        {
            tracing::debug!(namespace = %namespace, "Mount already exists for namespace");
            return;
        }

        tracing::info!(namespace = %namespace, "Creating mount for namespace");

        match self.mount_manager.mount_namespace(namespace).await {
            Ok(()) => {
                tracing::info!(namespace = %namespace, "Mount created successfully");
            }
            Err(e) => {
                tracing::error!(
                    namespace = %namespace,
                    error = %e,
                    "Failed to create mount for namespace"
                );
            }
        }
    }

    /// Handle namespace deletion - unmount if exists.
    async fn handle_namespace_deleted(&self, namespace: &str) {
        tracing::info!(namespace = %namespace, "User namespace deleted, checking mount");

        // Check if mount exists
        if self
            .mount_manager
            .get_mount_status(namespace)
            .await
            .is_none()
        {
            tracing::debug!(namespace = %namespace, "No mount exists for namespace");
            return;
        }

        tracing::info!(namespace = %namespace, "Unmounting for deleted namespace");

        match self.mount_manager.unmount_namespace(namespace).await {
            Ok(()) => {
                tracing::info!(namespace = %namespace, "Mount removed successfully");
            }
            Err(e) => {
                tracing::error!(
                    namespace = %namespace,
                    error = %e,
                    "Failed to unmount namespace"
                );
            }
        }
    }

    /// Stop the watcher.
    pub fn stop(&self) {
        self.cancel_token.cancel();
    }
}

/// Check if a namespace is a user namespace.
fn is_user_namespace(name: &str) -> bool {
    name.starts_with("u-")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_is_user_namespace() {
        assert!(is_user_namespace("u-alice"));
        assert!(is_user_namespace("u-bob-123"));
        assert!(is_user_namespace("u-github-434149"));

        assert!(!is_user_namespace("default"));
        assert!(!is_user_namespace("kube-system"));
        assert!(!is_user_namespace("basilica-storage"));
        assert!(!is_user_namespace("basilica-system"));
    }
}
