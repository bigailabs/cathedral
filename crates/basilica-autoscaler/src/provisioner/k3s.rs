/// K3s installation utilities
pub struct K3sInstaller;

/// Sanitize a shell word by removing potentially dangerous characters.
/// Allows alphanumeric, dash, underscore, and dot only.
/// Suitable for hostnames, node names, and general shell arguments.
fn sanitize_shell_word(s: &str) -> String {
    s.chars()
        .filter(|c| c.is_alphanumeric() || matches!(c, '-' | '_' | '.'))
        .collect()
}

/// Sanitize a Kubernetes label key.
/// Label keys can contain alphanumeric, dash, underscore, dot, and slash (for prefix).
/// See: https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/#syntax-and-character-set
fn sanitize_label_key(s: &str) -> String {
    s.chars()
        .filter(|c| c.is_alphanumeric() || matches!(c, '-' | '_' | '.' | '/'))
        .collect()
}

/// Sanitize a Kubernetes label value.
/// Label values can contain alphanumeric, dash, underscore, and dot only.
/// Must be 63 characters or less.
fn sanitize_label_value(s: &str) -> String {
    s.chars()
        .filter(|c| c.is_alphanumeric() || matches!(c, '-' | '_' | '.'))
        .take(63)
        .collect()
}

/// Escape a value for safe use in a single-quoted shell context.
/// This wraps the value in single quotes and escapes any single quotes within.
fn shell_escape(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\"'\"'"))
}

impl K3sInstaller {
    /// Generate K3s agent installation script
    /// All inputs are sanitized to prevent shell injection attacks.
    pub fn generate_install_script(
        server_url: &str,
        token: &str,
        node_name: &str,
        node_id: &str,
        flannel_interface: &str,
        extra_labels: &[(String, String)],
    ) -> String {
        // Sanitize inputs based on context
        let safe_node_name = sanitize_shell_word(node_name);
        let safe_node_id = sanitize_label_value(node_id);
        let safe_flannel = sanitize_shell_word(flannel_interface);

        let mut labels = vec![
            format!("--node-label=basilica.ai/node-id={}", safe_node_id),
            "--node-label=basilica.ai/managed-by=autoscaler".to_string(),
        ];

        for (key, value) in extra_labels {
            let safe_key = sanitize_label_key(key);
            let safe_value = sanitize_label_value(value);
            // Skip labels that become empty after sanitization
            if safe_key.is_empty() || safe_value.is_empty() {
                tracing::warn!(
                    original_key = %key,
                    original_value = %value,
                    "Skipping invalid label: sanitized key or value is empty"
                );
                continue;
            }
            labels.push(format!("--node-label={}={}", safe_key, safe_value));
        }

        let labels_str = labels.join(" ");

        // Use shell escaping for URL and token to handle special characters safely
        let escaped_url = shell_escape(server_url);
        let escaped_token = shell_escape(token);

        format!(
            r#"#!/bin/bash
set -euo pipefail

# Set hostname
hostnamectl set-hostname {safe_node_name}
# Idempotent /etc/hosts update: only add if not already present
grep -q "127.0.0.1 {safe_node_name}" /etc/hosts || echo "127.0.0.1 {safe_node_name}" >> /etc/hosts

# Install K3s agent
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="agent" \
    K3S_URL={escaped_url} \
    K3S_TOKEN={escaped_token} \
    sh -s - \
    --node-name={safe_node_name} \
    --flannel-iface={safe_flannel} \
    {labels_str}

# Wait for agent to be ready
sleep 10

# Verify installation
systemctl is-active k3s-agent || systemctl is-active k3s
"#,
            safe_node_name = safe_node_name,
            escaped_url = escaped_url,
            escaped_token = escaped_token,
            safe_flannel = safe_flannel,
            labels_str = labels_str
        )
    }

    /// Generate K3s uninstall script for agent
    pub fn generate_uninstall_script() -> &'static str {
        r#"#!/bin/bash
set -euo pipefail

# Stop K3s services
systemctl stop k3s-agent 2>/dev/null || true
systemctl stop k3s 2>/dev/null || true

# Run official uninstall script if available
if [ -f /usr/local/bin/k3s-agent-uninstall.sh ]; then
    /usr/local/bin/k3s-agent-uninstall.sh
elif [ -f /usr/local/bin/k3s-uninstall.sh ]; then
    /usr/local/bin/k3s-uninstall.sh
fi

# Clean up any remaining files
rm -rf /var/lib/rancher/k3s
rm -rf /etc/rancher/k3s
"#
    }

    /// Generate node labels from pool spec
    /// All values are sanitized to conform to Kubernetes label rules
    pub fn collect_node_labels(
        node_id: &str,
        pool_name: &str,
        gpu_type: Option<&str>,
        gpu_count: Option<u32>,
    ) -> Vec<(String, String)> {
        // Sanitize inputs - use "unknown" as fallback if sanitization results in empty string
        let safe_node_id = sanitize_label_value(node_id);
        let safe_node_id = if safe_node_id.is_empty() {
            "unknown".to_string()
        } else {
            safe_node_id
        };

        let safe_pool_name = sanitize_label_value(pool_name);
        let safe_pool_name = if safe_pool_name.is_empty() {
            "unknown".to_string()
        } else {
            safe_pool_name
        };

        let mut labels = vec![
            ("basilica.ai/node-id".to_string(), safe_node_id),
            (
                "basilica.ai/managed-by".to_string(),
                "autoscaler".to_string(),
            ),
            ("basilica.ai/nodepool".to_string(), safe_pool_name),
        ];

        if let Some(gpu) = gpu_type {
            let safe_gpu = sanitize_label_value(gpu);
            if !safe_gpu.is_empty() {
                labels.push(("nvidia.com/gpu.product".to_string(), safe_gpu));
            }
        }

        if let Some(count) = gpu_count {
            labels.push(("nvidia.com/gpu.count".to_string(), count.to_string()));
        }

        labels
    }
}

/// Verify K3s agent status by checking systemd service
pub fn verify_k3s_status(systemctl_output: &str) -> bool {
    systemctl_output.trim() == "active"
}

/// Parse K3s version output
pub fn parse_k3s_version(output: &str) -> Option<String> {
    // K3s version format: "k3s version v1.27.4+k3s1 (hash)"
    output
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(2))
        .map(|v| v.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generate_install_script_works() {
        let script = K3sInstaller::generate_install_script(
            "https://k3s.example.com:6443",
            "secret-token",
            "gpu-node-1",
            "node-123",
            "wg0",
            &[("custom-label".to_string(), "value".to_string())],
        );

        // URL and token now use single-quote escaping for safety
        assert!(script.contains("K3S_URL='https://k3s.example.com:6443'"));
        assert!(script.contains("K3S_TOKEN='secret-token'"));
        assert!(script.contains("--node-name=gpu-node-1"));
        assert!(script.contains("--flannel-iface=wg0"));
        assert!(script.contains("basilica.ai/node-id=node-123"));
        assert!(script.contains("custom-label=value"));
    }

    #[test]
    fn sanitize_shell_word_removes_dangerous_chars() {
        assert_eq!(sanitize_shell_word("node-123"), "node-123");
        assert_eq!(sanitize_shell_word("node_name.local"), "node_name.local");
        assert_eq!(sanitize_shell_word("$(rm -rf /)"), "rm-rf");
        assert_eq!(sanitize_shell_word("test`whoami`"), "testwhoami");
        assert_eq!(sanitize_shell_word("test;echo"), "testecho");
        assert_eq!(sanitize_shell_word("label=value"), "labelvalue");
    }

    #[test]
    fn sanitize_label_key_allows_slash_for_prefix() {
        assert_eq!(
            sanitize_label_key("basilica.ai/node-id"),
            "basilica.ai/node-id"
        );
        assert_eq!(
            sanitize_label_key("kubernetes.io/arch"),
            "kubernetes.io/arch"
        );
        assert_eq!(sanitize_label_key("app"), "app");
        assert_eq!(sanitize_label_key("app=value"), "appvalue");
    }

    #[test]
    fn sanitize_label_value_enforces_k8s_rules() {
        assert_eq!(sanitize_label_value("node-123"), "node-123");
        assert_eq!(sanitize_label_value("my_value.test"), "my_value.test");
        assert_eq!(sanitize_label_value("no:colons"), "nocolons");
        assert_eq!(sanitize_label_value("no=equals"), "noequals");
        assert_eq!(sanitize_label_value("no/slashes"), "noslashes");
    }

    #[test]
    fn sanitize_label_value_truncates_to_63_chars() {
        let long_value = "a".repeat(100);
        assert_eq!(sanitize_label_value(&long_value).len(), 63);
    }

    #[test]
    fn shell_escape_handles_special_chars() {
        assert_eq!(shell_escape("simple"), "'simple'");
        assert_eq!(shell_escape("with'quote"), "'with'\"'\"'quote'");
    }

    #[test]
    fn collect_node_labels_includes_all() {
        let labels =
            K3sInstaller::collect_node_labels("node-abc", "pool-1", Some("RTX-4090"), Some(2));

        assert!(labels
            .iter()
            .any(|(k, v)| k == "basilica.ai/node-id" && v == "node-abc"));
        assert!(labels
            .iter()
            .any(|(k, v)| k == "basilica.ai/nodepool" && v == "pool-1"));
        assert!(labels
            .iter()
            .any(|(k, v)| k == "nvidia.com/gpu.product" && v == "RTX-4090"));
        assert!(labels
            .iter()
            .any(|(k, v)| k == "nvidia.com/gpu.count" && v == "2"));
    }

    #[test]
    fn verify_k3s_status_works() {
        assert!(verify_k3s_status("active"));
        assert!(verify_k3s_status("active\n"));
        assert!(!verify_k3s_status("inactive"));
        assert!(!verify_k3s_status("failed"));
    }

    #[test]
    fn parse_k3s_version_works() {
        let output = "k3s version v1.27.4+k3s1 (abc123)";
        assert_eq!(parse_k3s_version(output), Some("v1.27.4+k3s1".to_string()));

        assert_eq!(parse_k3s_version(""), None);
    }
}
