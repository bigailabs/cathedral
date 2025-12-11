/// K3s installation utilities
pub struct K3sInstaller;

impl K3sInstaller {
    /// Generate K3s agent installation script
    pub fn generate_install_script(
        server_url: &str,
        token: &str,
        node_name: &str,
        node_id: &str,
        flannel_interface: &str,
        extra_labels: &[(String, String)],
    ) -> String {
        let mut labels = vec![
            format!("--node-label=basilica.ai/node-id={}", node_id),
            "--node-label=basilica.ai/managed-by=autoscaler".to_string(),
        ];

        for (key, value) in extra_labels {
            labels.push(format!("--node-label={}={}", key, value));
        }

        let labels_str = labels.join(" ");

        format!(
            r#"#!/bin/bash
set -euo pipefail

# Set hostname
hostnamectl set-hostname {node_name}
echo "127.0.0.1 {node_name}" >> /etc/hosts

# Install K3s agent
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="agent" \
    K3S_URL="{server_url}" \
    K3S_TOKEN="{token}" \
    sh -s - \
    --node-name={node_name} \
    --flannel-iface={flannel_interface} \
    {labels_str}

# Wait for agent to be ready
sleep 10

# Verify installation
systemctl is-active k3s-agent || systemctl is-active k3s
"#,
            node_name = node_name,
            server_url = server_url,
            token = token,
            flannel_interface = flannel_interface,
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
    pub fn collect_node_labels(
        node_id: &str,
        pool_name: &str,
        gpu_type: Option<&str>,
        gpu_count: Option<u32>,
    ) -> Vec<(String, String)> {
        let mut labels = vec![
            ("basilica.ai/node-id".to_string(), node_id.to_string()),
            (
                "basilica.ai/managed-by".to_string(),
                "autoscaler".to_string(),
            ),
            ("basilica.ai/nodepool".to_string(), pool_name.to_string()),
        ];

        if let Some(gpu) = gpu_type {
            labels.push(("nvidia.com/gpu.product".to_string(), gpu.to_string()));
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

        assert!(script.contains("K3S_URL=\"https://k3s.example.com:6443\""));
        assert!(script.contains("K3S_TOKEN=\"secret-token\""));
        assert!(script.contains("--node-name=gpu-node-1"));
        assert!(script.contains("--flannel-iface=wg0"));
        assert!(script.contains("basilica.ai/node-id=node-123"));
        assert!(script.contains("custom-label=value"));
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
