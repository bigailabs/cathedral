//! Kubernetes integration test helpers
//!
//! This module provides utilities for setting up and managing Kubernetes
//! integration tests, including namespace creation, cleanup, and test fixtures.

use anyhow::{Context, Result};
use std::env;

/// Test context for Kubernetes integration tests
///
/// Provides isolated namespace and cleanup on drop
pub struct K8sTestContext {
    pub client: kube::Client,
    pub namespace: String,
    cleanup_on_drop: bool,
}

impl K8sTestContext {
    /// Create a new test context with an isolated namespace
    ///
    /// The namespace will be created with a unique name based on the test name.
    /// By default, the namespace is cleaned up when the context is dropped.
    pub async fn new(test_name: &str) -> Result<Self> {
        let client = Self::get_client().await?;
        let namespace = Self::create_test_namespace(&client, test_name).await?;

        Ok(Self {
            client,
            namespace,
            cleanup_on_drop: true,
        })
    }

    /// Create a test context using an existing namespace (no cleanup)
    ///
    /// This is useful for running tests against a shared E2E environment
    /// where the namespace is managed externally.
    pub async fn with_existing_namespace(namespace: impl Into<String>) -> Result<Self> {
        let client = Self::get_client().await?;
        let namespace = namespace.into();

        // Verify namespace exists
        use k8s_openapi::api::core::v1::Namespace;
        use kube::Api;
        let ns_api: Api<Namespace> = Api::all(client.clone());
        ns_api
            .get(&namespace)
            .await
            .context(format!("Namespace '{}' does not exist", namespace))?;

        Ok(Self {
            client,
            namespace,
            cleanup_on_drop: false,
        })
    }

    /// Disable cleanup on drop (useful for debugging failed tests)
    pub fn keep_namespace_on_drop(mut self) -> Self {
        self.cleanup_on_drop = false;
        self
    }

    /// Get Kubernetes client from environment or in-cluster config
    async fn get_client() -> Result<kube::Client> {
        // Try KUBECONFIG env var first (E2E environment)
        if let Ok(kubeconfig_path) = env::var("KUBECONFIG") {
            tracing::debug!("Using KUBECONFIG: {}", kubeconfig_path);
            let config = kube::Config::from_kubeconfig(&kube::config::KubeConfigOptions {
                context: None,
                cluster: None,
                user: None,
            })
            .await
            .context("Failed to load kubeconfig")?;
            return kube::Client::try_from(config).context("Failed to create K8s client");
        }

        // Fall back to in-cluster or default kubeconfig
        kube::Client::try_default()
            .await
            .context("Failed to create K8s client from default config")
    }

    /// Create a unique test namespace
    async fn create_test_namespace(client: &kube::Client, test_name: &str) -> Result<String> {
        use k8s_openapi::api::core::v1::Namespace;
        use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
        use kube::api::PostParams;
        use kube::Api;

        let namespace_name = format!("test-{}-{}", test_name, uuid::Uuid::new_v4())
            .chars()
            .take(63) // K8s name limit
            .collect::<String>()
            .trim_end_matches('-')
            .to_string();

        let ns_api: Api<Namespace> = Api::all(client.clone());
        let ns = Namespace {
            metadata: ObjectMeta {
                name: Some(namespace_name.clone()),
                labels: Some(
                    vec![("basilica.ai/test".to_string(), "true".to_string())]
                        .into_iter()
                        .collect(),
                ),
                ..Default::default()
            },
            ..Default::default()
        };

        ns_api
            .create(&PostParams::default(), &ns)
            .await
            .context(format!("Failed to create namespace '{}'", namespace_name))?;

        tracing::info!("Created test namespace: {}", namespace_name);
        Ok(namespace_name)
    }

    /// Load a test fixture from file and parse as JSON
    pub fn load_fixture_json<T>(&self, fixture_name: &str) -> Result<T>
    where
        T: serde::de::DeserializeOwned,
    {
        let fixture_path = format!("../../scripts/e2e/fixtures/{}", fixture_name);
        let content = std::fs::read_to_string(&fixture_path)
            .context(format!("Failed to read fixture: {}", fixture_path))?;
        serde_json::from_str(&content)
            .context(format!("Failed to parse JSON fixture: {}", fixture_path))
    }

    /// Load a test fixture from file and parse as YAML
    pub fn load_fixture_yaml<T>(&self, fixture_name: &str) -> Result<T>
    where
        T: serde::de::DeserializeOwned,
    {
        let fixture_path = format!("../../scripts/e2e/fixtures/{}", fixture_name);
        let content = std::fs::read_to_string(&fixture_path)
            .context(format!("Failed to read fixture: {}", fixture_path))?;
        serde_yaml::from_str(&content)
            .context(format!("Failed to parse YAML fixture: {}", fixture_path))
    }

    /// Wait for a condition with timeout
    pub async fn wait_for<F, Fut>(
        &self,
        condition: F,
        timeout_secs: u64,
        poll_interval_ms: u64,
    ) -> Result<()>
    where
        F: Fn() -> Fut,
        Fut: std::future::Future<Output = Result<bool>>,
    {
        use tokio::time::{sleep, Duration};

        let start = std::time::Instant::now();
        let timeout = Duration::from_secs(timeout_secs);

        loop {
            if condition().await? {
                return Ok(());
            }

            if start.elapsed() > timeout {
                anyhow::bail!("Timeout waiting for condition after {}s", timeout_secs);
            }

            sleep(Duration::from_millis(poll_interval_ms)).await;
        }
    }

    /// Check if test should be skipped (cluster not available)
    pub async fn should_skip_test() -> bool {
        // Skip if NO_K8S_TESTS env var is set
        if env::var("NO_K8S_TESTS").is_ok() {
            tracing::warn!("Skipping K8s integration test (NO_K8S_TESTS set)");
            return true;
        }

        // Skip if cluster is not reachable
        match kube::Client::try_default().await {
            Ok(client) => {
                // Try to list namespaces as a connectivity check
                use k8s_openapi::api::core::v1::Namespace;
                use kube::api::ListParams;
                use kube::Api;
                let ns_api: Api<Namespace> = Api::all(client);
                match ns_api.list(&ListParams::default().limit(1)).await {
                    Ok(_) => false, // Cluster is reachable
                    Err(e) => {
                        tracing::warn!(
                            "Skipping K8s integration test (cluster not reachable): {}",
                            e
                        );
                        true
                    }
                }
            }
            Err(e) => {
                tracing::warn!("Skipping K8s integration test (no K8s config): {}", e);
                true
            }
        }
    }
}

impl Drop for K8sTestContext {
    fn drop(&mut self) {
        if !self.cleanup_on_drop {
            tracing::info!("Keeping test namespace: {}", self.namespace);
            return;
        }

        // Clean up namespace asynchronously
        let namespace = self.namespace.clone();
        let client = self.client.clone();

        // Spawn cleanup task (best effort)
        tokio::spawn(async move {
            use k8s_openapi::api::core::v1::Namespace;
            use kube::api::DeleteParams;
            use kube::Api;

            let ns_api: Api<Namespace> = Api::all(client);
            match ns_api.delete(&namespace, &DeleteParams::default()).await {
                Ok(_) => tracing::info!("Deleted test namespace: {}", namespace),
                Err(e) => tracing::warn!("Failed to delete test namespace '{}': {}", namespace, e),
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_context_should_skip() {
        // This just tests that the function doesn't panic
        let _ = K8sTestContext::should_skip_test().await;
    }
}
