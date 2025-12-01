use anyhow::{Context, Result};
use k8s_openapi::api::core::v1::{LimitRange, ServiceAccount};
use k8s_openapi::api::networking::v1::NetworkPolicy;
use k8s_openapi::api::rbac::v1::{Role, RoleBinding};
use kube::api::{Api, PostParams};
use kube::Client;
use serde::Deserialize;

const RBAC_TEMPLATE: &str = include_str!("policies/rbac-template.yaml");
const NETPOL_DEFAULT_DENY_TEMPLATE: &str = include_str!("policies/netpol-default-deny.yaml");
const NETPOL_ALLOW_DNS_TEMPLATE: &str = include_str!("policies/netpol-allow-dns.yaml");
const NETPOL_ALLOW_INTERNET_TEMPLATE: &str = include_str!("policies/netpol-allow-internet.yaml");
const NETPOL_ALLOW_INGRESS_TEMPLATE: &str = include_str!("policies/netpol-allow-ingress.yaml");
const LIMITRANGE_TEMPLATE: &str = include_str!("policies/limitrange-template.yaml");

fn validate_namespace(namespace: &str) -> Result<()> {
    if namespace.len() > 63 || namespace.is_empty() {
        anyhow::bail!("Namespace must be 1-63 characters, got {}", namespace.len());
    }

    if !namespace
        .chars()
        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
    {
        anyhow::bail!("Namespace must contain only lowercase letters, digits, and hyphens");
    }

    if namespace.starts_with('-') || namespace.ends_with('-') {
        anyhow::bail!("Namespace cannot start or end with hyphen");
    }

    if namespace.contains("TENANT_NAMESPACE") {
        anyhow::bail!("Namespace cannot contain template placeholders");
    }

    Ok(())
}

pub async fn apply_user_namespace_security_policies(
    client: &Client,
    namespace: &str,
) -> Result<()> {
    validate_namespace(namespace).context("Invalid namespace for security policy application")?;

    tracing::info!(
        target: "security_audit",
        event_type = "security_policy_application_started",
        severity = "info",
        namespace = %namespace,
        "Applying security policies to user namespace"
    );

    apply_rbac_policies(client, namespace)
        .await
        .context("Failed to apply RBAC policies")?;

    apply_network_policies(client, namespace)
        .await
        .context("Failed to apply NetworkPolicies")?;

    apply_limit_range(client, namespace)
        .await
        .context("Failed to apply LimitRange")?;

    tracing::info!(
        target: "security_audit",
        event_type = "security_policy_application_completed",
        severity = "info",
        namespace = %namespace,
        policies_applied = "RBAC,NetworkPolicies,LimitRange",
        "Security policies successfully applied to user namespace"
    );

    Ok(())
}

async fn apply_rbac_policies(client: &Client, namespace: &str) -> Result<()> {
    let yaml_content = RBAC_TEMPLATE.replace("TENANT_NAMESPACE", namespace);

    let documents: Vec<serde_yaml::Value> = serde_yaml::Deserializer::from_str(&yaml_content)
        .filter_map(|doc| match serde_yaml::Value::deserialize(doc) {
            Ok(value) if !value.is_null() => Some(value),
            Ok(_) => None,
            Err(e) => {
                tracing::error!(
                    error = %e,
                    namespace = %namespace,
                    "Failed to deserialize YAML document in RBAC template"
                );
                None
            }
        })
        .collect();

    for doc in documents {
        let kind = doc
            .get("kind")
            .and_then(|k| k.as_str())
            .context("Missing 'kind' field in YAML document")?;

        match kind {
            "ServiceAccount" => {
                let sa: ServiceAccount =
                    serde_yaml::from_value(doc).context("Failed to deserialize ServiceAccount")?;
                let api: Api<ServiceAccount> = Api::namespaced(client.clone(), namespace);

                match api.create(&PostParams::default(), &sa).await {
                    Ok(_) => {
                        tracing::debug!(
                            namespace = %namespace,
                            resource_type = "ServiceAccount",
                            resource_name = sa.metadata.name.as_deref().unwrap_or("unknown"),
                            "Created ServiceAccount"
                        );
                    }
                    Err(kube::Error::Api(ae)) if ae.code == 409 => {
                        tracing::debug!(
                            namespace = %namespace,
                            resource_type = "ServiceAccount",
                            "ServiceAccount already exists"
                        );
                    }
                    Err(e) => {
                        tracing::warn!(
                            error = %e,
                            namespace = %namespace,
                            resource_type = "ServiceAccount",
                            "Failed to create ServiceAccount, continuing anyway"
                        );
                    }
                }
            }
            "Role" => {
                let role: Role =
                    serde_yaml::from_value(doc).context("Failed to deserialize Role")?;
                let api: Api<Role> = Api::namespaced(client.clone(), namespace);

                match api.create(&PostParams::default(), &role).await {
                    Ok(_) => {
                        tracing::debug!(
                            namespace = %namespace,
                            resource_type = "Role",
                            resource_name = role.metadata.name.as_deref().unwrap_or("unknown"),
                            "Created Role"
                        );
                    }
                    Err(kube::Error::Api(ae)) if ae.code == 409 => {
                        tracing::debug!(
                            namespace = %namespace,
                            resource_type = "Role",
                            "Role already exists"
                        );
                    }
                    Err(e) => {
                        tracing::warn!(
                            error = %e,
                            namespace = %namespace,
                            resource_type = "Role",
                            "Failed to create Role, continuing anyway"
                        );
                    }
                }
            }
            "RoleBinding" => {
                let rb: RoleBinding =
                    serde_yaml::from_value(doc).context("Failed to deserialize RoleBinding")?;
                let api: Api<RoleBinding> = Api::namespaced(client.clone(), namespace);

                match api.create(&PostParams::default(), &rb).await {
                    Ok(_) => {
                        tracing::debug!(
                            namespace = %namespace,
                            resource_type = "RoleBinding",
                            resource_name = rb.metadata.name.as_deref().unwrap_or("unknown"),
                            "Created RoleBinding"
                        );
                    }
                    Err(kube::Error::Api(ae)) if ae.code == 409 => {
                        tracing::debug!(
                            namespace = %namespace,
                            resource_type = "RoleBinding",
                            "RoleBinding already exists"
                        );
                    }
                    Err(e) => {
                        tracing::warn!(
                            error = %e,
                            namespace = %namespace,
                            resource_type = "RoleBinding",
                            "Failed to create RoleBinding, continuing anyway"
                        );
                    }
                }
            }
            _ => {
                tracing::warn!(
                    kind = %kind,
                    namespace = %namespace,
                    "Unsupported resource kind in RBAC template"
                );
            }
        }
    }

    Ok(())
}

async fn apply_network_policies(client: &Client, namespace: &str) -> Result<()> {
    let templates = [
        ("default-deny-all", NETPOL_DEFAULT_DENY_TEMPLATE),
        ("allow-dns", NETPOL_ALLOW_DNS_TEMPLATE),
        ("allow-internet-egress", NETPOL_ALLOW_INTERNET_TEMPLATE),
        ("allow-ingress-from-envoy", NETPOL_ALLOW_INGRESS_TEMPLATE),
    ];

    for (policy_name, template) in &templates {
        let yaml_content = template.replace("TENANT_NAMESPACE", namespace);

        let netpol: NetworkPolicy = serde_yaml::from_str(&yaml_content)
            .with_context(|| format!("Failed to deserialize NetworkPolicy: {}", policy_name))?;

        let api: Api<NetworkPolicy> = Api::namespaced(client.clone(), namespace);

        match api.create(&PostParams::default(), &netpol).await {
            Ok(_) => {
                tracing::debug!(
                    namespace = %namespace,
                    policy_name = %policy_name,
                    "Created NetworkPolicy"
                );
            }
            Err(kube::Error::Api(ae)) if ae.code == 409 => {
                tracing::debug!(
                    namespace = %namespace,
                    policy_name = %policy_name,
                    "NetworkPolicy already exists"
                );
            }
            Err(e) => {
                tracing::warn!(
                    error = %e,
                    namespace = %namespace,
                    policy_name = %policy_name,
                    "Failed to create NetworkPolicy, continuing anyway"
                );
            }
        }
    }

    Ok(())
}

async fn apply_limit_range(client: &Client, namespace: &str) -> Result<()> {
    let yaml_content = LIMITRANGE_TEMPLATE.replace("TENANT_NAMESPACE", namespace);

    let limit_range: LimitRange = serde_yaml::from_str(&yaml_content)
        .context("Failed to deserialize LimitRange template")?;

    let api: Api<LimitRange> = Api::namespaced(client.clone(), namespace);

    match api.create(&PostParams::default(), &limit_range).await {
        Ok(_) => {
            tracing::debug!(
                namespace = %namespace,
                resource_type = "LimitRange",
                "Created LimitRange"
            );
        }
        Err(kube::Error::Api(ae)) if ae.code == 409 => {
            tracing::debug!(
                namespace = %namespace,
                resource_type = "LimitRange",
                "LimitRange already exists"
            );
        }
        Err(e) => {
            tracing::warn!(
                error = %e,
                namespace = %namespace,
                resource_type = "LimitRange",
                "Failed to create LimitRange, continuing anyway"
            );
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_rbac_template_replacement() {
        let namespace = "u-test-user";
        let yaml_content = RBAC_TEMPLATE.replace("TENANT_NAMESPACE", namespace);

        assert!(yaml_content.contains("namespace: u-test-user"));
        assert!(!yaml_content.contains("TENANT_NAMESPACE"));

        // Verify fuse-daemon RoleBinding is present
        assert!(yaml_content.contains("name: fuse-daemon-secret-reader"));
        assert!(yaml_content.contains("namespace: basilica-storage"));
    }

    #[test]
    fn test_netpol_template_replacement() {
        let namespace = "u-alice";
        let yaml_content = NETPOL_DEFAULT_DENY_TEMPLATE.replace("TENANT_NAMESPACE", namespace);

        assert!(yaml_content.contains("namespace: u-alice"));
        assert!(!yaml_content.contains("TENANT_NAMESPACE"));
    }

    #[test]
    #[allow(clippy::const_is_empty)]
    fn test_all_templates_embedded() {
        assert!(!RBAC_TEMPLATE.is_empty());
        assert!(!NETPOL_DEFAULT_DENY_TEMPLATE.is_empty());
        assert!(!NETPOL_ALLOW_DNS_TEMPLATE.is_empty());
        assert!(!NETPOL_ALLOW_INTERNET_TEMPLATE.is_empty());
        assert!(!NETPOL_ALLOW_INGRESS_TEMPLATE.is_empty());
        assert!(!LIMITRANGE_TEMPLATE.is_empty());
    }

    #[test]
    fn test_limitrange_template_replacement() {
        let namespace = "u-gpu-user";
        let yaml_content = LIMITRANGE_TEMPLATE.replace("TENANT_NAMESPACE", namespace);

        assert!(yaml_content.contains("namespace: u-gpu-user"));
        assert!(!yaml_content.contains("TENANT_NAMESPACE"));
        assert!(yaml_content.contains("cpu: \"128\""));
        assert!(yaml_content.contains("memory: 512Gi"));
    }

    #[test]
    fn test_validate_namespace_valid() {
        assert!(validate_namespace("u-test-user").is_ok());
        assert!(validate_namespace("u-alice").is_ok());
        assert!(validate_namespace("u-test-123").is_ok());
        assert!(validate_namespace("namespace-with-dashes").is_ok());
    }

    #[test]
    fn test_validate_namespace_too_long() {
        let long_namespace = "a".repeat(64);
        assert!(validate_namespace(&long_namespace).is_err());
    }

    #[test]
    fn test_validate_namespace_empty() {
        assert!(validate_namespace("").is_err());
    }

    #[test]
    fn test_validate_namespace_invalid_chars() {
        assert!(validate_namespace("U-Test").is_err());
        assert!(validate_namespace("u_test").is_err());
        assert!(validate_namespace("u.test").is_err());
        assert!(validate_namespace("u test").is_err());
    }

    #[test]
    fn test_validate_namespace_starts_with_hyphen() {
        assert!(validate_namespace("-test").is_err());
    }

    #[test]
    fn test_validate_namespace_ends_with_hyphen() {
        assert!(validate_namespace("test-").is_err());
    }

    #[test]
    fn test_validate_namespace_injection_attempt() {
        assert!(validate_namespace("TENANT_NAMESPACE").is_err());
        assert!(validate_namespace("u-TENANT_NAMESPACE").is_err());
    }
}
