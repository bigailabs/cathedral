use async_trait::async_trait;
use std::time::Duration;
use tracing::{debug, error, info, warn};

use crate::api::WireGuardPeer;
use crate::crd::{K3sConfig, WireGuardConfig};
use crate::error::{AutoscalerError, Result};
use crate::provisioner::k3s::{parse_k3s_version, verify_k3s_status};
use crate::provisioner::wireguard::{generate_wg_config, parse_wg_show_output};

/// SSH connection configuration
#[derive(Clone)]
pub struct SshConnectionConfig {
    pub username: String,
    pub private_key: String,
    pub passphrase: Option<String>,
}

/// Trait for node provisioning operations
#[async_trait]
pub trait NodeProvisioner: Send + Sync {
    /// Configure base system (update packages, install dependencies)
    async fn configure_base_system(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
    ) -> Result<()>;

    /// Install WireGuard and generate keypair
    async fn install_wireguard(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
        node_ip: &str,
        config: &WireGuardConfig,
    ) -> Result<String>; // Returns public key

    /// Configure WireGuard peers
    async fn configure_wireguard_peers(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
        peers: &[WireGuardPeer],
    ) -> Result<()>;

    /// Validate WireGuard connectivity
    async fn validate_wireguard_connectivity(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
    ) -> Result<bool>;

    /// Validate control plane connectivity (ping control plane IPs and check K3s API)
    async fn validate_control_plane_connectivity(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
        control_plane_ips: &[&str],
        api_server_url: &str,
    ) -> Result<()>;

    /// Install K3s agent and join cluster
    async fn install_k3s_agent(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
        k3s_config: &K3sConfig,
        token: &str,
        node_id: &str,
    ) -> Result<String>; // Returns k8s node name

    /// Execute lifecycle script (preJoinScript or postJoinScript)
    async fn execute_lifecycle_script(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
        script: &str,
        script_type: &str,
    ) -> Result<()>;
}

/// SSH-based node provisioner implementation
#[derive(Clone)]
pub struct SshProvisioner {
    connection_timeout: Duration,
    execution_timeout: Duration,
    max_retries: u32,
    retry_delay: Duration,
}

impl SshProvisioner {
    pub fn new(
        connection_timeout: Duration,
        execution_timeout: Duration,
        max_retries: u32,
        retry_delay: Duration,
    ) -> Self {
        Self {
            connection_timeout,
            execution_timeout,
            max_retries,
            retry_delay,
        }
    }

    pub fn from_config(config: &crate::config::SshConfig) -> Self {
        Self {
            connection_timeout: config.connection_timeout,
            execution_timeout: config.execution_timeout,
            max_retries: config.max_retries,
            retry_delay: config.retry_delay,
        }
    }

    async fn execute_ssh_command(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
        command: &str,
    ) -> Result<String> {
        use async_ssh2_tokio::{AuthMethod, Client, ServerCheckMethod};

        let auth = AuthMethod::with_key(&ssh_config.private_key, ssh_config.passphrase.as_deref());

        let mut last_error = None;
        for attempt in 0..self.max_retries {
            if attempt > 0 {
                debug!(host = %host, attempt = %attempt, "Retrying SSH connection");
                tokio::time::sleep(self.retry_delay).await;
            }

            match tokio::time::timeout(
                self.connection_timeout,
                Client::connect(
                    (host, port),
                    &ssh_config.username,
                    auth.clone(),
                    ServerCheckMethod::NoCheck,
                ),
            )
            .await
            {
                Ok(Ok(client)) => {
                    debug!(host = %host, command = %command, "Executing SSH command");
                    match tokio::time::timeout(self.execution_timeout, client.execute(command))
                        .await
                    {
                        Ok(Ok(result)) => {
                            if result.exit_status != 0 {
                                return Err(AutoscalerError::SshExecution {
                                    command: command.to_string(),
                                    exit_code: result.exit_status,
                                    stderr: result.stderr,
                                });
                            }
                            return Ok(result.stdout);
                        }
                        Ok(Err(e)) => {
                            last_error = Some(AutoscalerError::SshConnection {
                                host: host.to_string(),
                                reason: e.to_string(),
                            });
                        }
                        Err(_) => {
                            last_error = Some(AutoscalerError::SshConnection {
                                host: host.to_string(),
                                reason: "Command execution timed out".to_string(),
                            });
                        }
                    }
                }
                Ok(Err(e)) => {
                    last_error = Some(AutoscalerError::SshConnection {
                        host: host.to_string(),
                        reason: e.to_string(),
                    });
                }
                Err(_) => {
                    last_error = Some(AutoscalerError::SshConnection {
                        host: host.to_string(),
                        reason: "Connection timed out".to_string(),
                    });
                }
            }
        }

        Err(
            last_error.unwrap_or_else(|| AutoscalerError::SshConnection {
                host: host.to_string(),
                reason: "Unknown error".to_string(),
            }),
        )
    }

    async fn upload_file(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
        content: &str,
        remote_path: &str,
        mode: &str,
    ) -> Result<()> {
        use base64::Engine;
        // Use base64 encoding to avoid heredoc delimiter collision and command injection
        let encoded = base64::engine::general_purpose::STANDARD.encode(content);
        let command = format!(
            "echo '{}' | base64 -d > {} && chmod {} {}",
            encoded, remote_path, mode, remote_path
        );
        self.execute_ssh_command(host, port, ssh_config, &command)
            .await?;
        Ok(())
    }

    async fn verify_flannel_interface(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
        node_name: &str,
    ) -> Result<()> {
        const MAX_ATTEMPTS: u32 = 30;
        const POLL_INTERVAL: Duration = Duration::from_secs(2);

        info!(host = %host, "Verifying flannel.1 interface");

        for attempt in 1..=MAX_ATTEMPTS {
            let output = self
                .execute_ssh_command(
                    host,
                    port,
                    ssh_config,
                    "ip link show flannel.1 2>/dev/null || true",
                )
                .await?;

            if output.contains("state UP") || output.contains("state UNKNOWN") {
                info!(host = %host, attempt = %attempt, "flannel.1 interface is UP");
                return Ok(());
            }

            debug!(host = %host, attempt = %attempt, "flannel.1 not ready yet, waiting...");
            tokio::time::sleep(POLL_INTERVAL).await;
        }

        Err(AutoscalerError::FlannelTimeout {
            node: node_name.to_string(),
            attempts: MAX_ATTEMPTS,
        })
    }

    async fn validate_mtu_settings(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
    ) -> Result<()> {
        // Validate WireGuard MTU (should be 1420)
        let wg_mtu = self
            .execute_ssh_command(
                host,
                port,
                ssh_config,
                "ip link show wg0 | grep -oP 'mtu \\K[0-9]+'",
            )
            .await?;
        let wg_mtu_val: u32 = wg_mtu.trim().parse().unwrap_or(0);
        if wg_mtu_val != 1420 {
            warn!(host = %host, mtu = %wg_mtu_val, expected = 1420, "WireGuard MTU mismatch");
        }

        // Validate Flannel MTU (should be 1370 = 1420 - 50 for VXLAN overhead)
        let flannel_mtu = self
            .execute_ssh_command(
                host,
                port,
                ssh_config,
                "ip link show flannel.1 | grep -oP 'mtu \\K[0-9]+'",
            )
            .await?;
        let flannel_mtu_val: u32 = flannel_mtu.trim().parse().unwrap_or(0);
        if flannel_mtu_val != 1370 {
            warn!(host = %host, mtu = %flannel_mtu_val, expected = 1370, "Flannel MTU mismatch");
        }

        info!(host = %host, wg_mtu = %wg_mtu_val, flannel_mtu = %flannel_mtu_val, "MTU settings validated");
        Ok(())
    }
}

#[async_trait]
impl NodeProvisioner for SshProvisioner {
    async fn configure_base_system(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
    ) -> Result<()> {
        info!(host = %host, "Configuring base system");

        // Update package lists and install essential packages
        let commands = [
            "export DEBIAN_FRONTEND=noninteractive",
            "apt-get update -qq",
            "apt-get install -y -qq curl wget gnupg2 ca-certificates lsb-release apt-transport-https software-properties-common",
        ];

        for cmd in commands {
            self.execute_ssh_command(host, port, ssh_config, cmd)
                .await?;
        }

        // Set hostname based on node ID (will be done in K3s install)
        info!(host = %host, "Base system configuration complete");
        Ok(())
    }

    async fn install_wireguard(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
        node_ip: &str,
        _config: &WireGuardConfig,
    ) -> Result<String> {
        info!(host = %host, "Installing WireGuard");

        // Install WireGuard
        self.execute_ssh_command(
            host,
            port,
            ssh_config,
            "apt-get install -y -qq wireguard wireguard-tools",
        )
        .await?;

        // Generate keypair
        self.execute_ssh_command(
            host,
            port,
            ssh_config,
            "wg genkey | tee /etc/wireguard/private.key | wg pubkey > /etc/wireguard/public.key && chmod 600 /etc/wireguard/private.key",
        )
        .await?;

        // Get public key
        let public_key = self
            .execute_ssh_command(host, port, ssh_config, "cat /etc/wireguard/public.key")
            .await?
            .trim()
            .to_string();

        // Get private key
        let private_key = self
            .execute_ssh_command(host, port, ssh_config, "cat /etc/wireguard/private.key")
            .await?
            .trim()
            .to_string();

        // Create WireGuard config using utility function (peers added later)
        let interface_name = "wg0";
        let listen_port: u16 = 51820;
        let wg_config = generate_wg_config(&private_key, node_ip, listen_port, interface_name, &[]);

        self.upload_file(
            host,
            port,
            ssh_config,
            &wg_config,
            &format!("/etc/wireguard/{}.conf", interface_name),
            "600",
        )
        .await?;

        // Enable IP forwarding
        self.execute_ssh_command(
            host,
            port,
            ssh_config,
            "echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf && sysctl -p",
        )
        .await?;

        // Enable and start WireGuard
        let enable_cmd = format!(
            "systemctl enable wg-quick@{} && systemctl start wg-quick@{}",
            interface_name, interface_name
        );
        self.execute_ssh_command(host, port, ssh_config, &enable_cmd)
            .await?;

        let key_prefix = public_key.get(..8).unwrap_or(&public_key);
        info!(host = %host, "WireGuard installed, public key: {}", key_prefix);
        Ok(public_key)
    }

    async fn configure_wireguard_peers(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
        peers: &[WireGuardPeer],
    ) -> Result<()> {
        info!(host = %host, peer_count = %peers.len(), "Configuring WireGuard peers");

        for peer in peers {
            use base64::Engine;
            let peer_config = format!(
                r#"
[Peer]
PublicKey = {}
AllowedIPs = {}
Endpoint = {}
PersistentKeepalive = 25
"#,
                peer.public_key, peer.allowed_ips, peer.endpoint
            );

            // Use base64 encoding to avoid heredoc delimiter collision
            let encoded = base64::engine::general_purpose::STANDARD.encode(&peer_config);
            let cmd = format!("echo '{}' | base64 -d >> /etc/wireguard/wg0.conf", encoded);
            self.execute_ssh_command(host, port, ssh_config, &cmd)
                .await?;
        }

        // Restart WireGuard to apply changes
        self.execute_ssh_command(host, port, ssh_config, "systemctl restart wg-quick@wg0")
            .await?;

        info!(host = %host, "WireGuard peers configured");
        Ok(())
    }

    async fn validate_wireguard_connectivity(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
    ) -> Result<bool> {
        info!(host = %host, "Validating WireGuard connectivity");

        // Check WireGuard interface is up
        let output = self
            .execute_ssh_command(host, port, ssh_config, "wg show wg0")
            .await?;

        if !output.contains("interface: wg0") {
            warn!(host = %host, "WireGuard interface wg0 not found");
            return Ok(false);
        }

        // Parse peer status for detailed logging
        let peers = parse_wg_show_output(&output);
        for peer in &peers {
            // Use safe slice to avoid panic on short/malformed keys
            let key_prefix = peer.public_key.get(..8).unwrap_or(&peer.public_key);
            debug!(
                host = %host,
                peer_key = %key_prefix,
                endpoint = ?peer.endpoint,
                "WireGuard peer status"
            );
        }

        // Check for recent handshake (within last 3 minutes)
        let handshake_check = self
            .execute_ssh_command(
                host,
                port,
                ssh_config,
                "wg show wg0 latest-handshakes | head -1 | awk '{print $2}'",
            )
            .await?;

        if let Ok(timestamp) = handshake_check.trim().parse::<u64>() {
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_secs();
            let age = now.saturating_sub(timestamp);
            if age < 180 {
                info!(host = %host, connected_peers = %peers.len(), "WireGuard connectivity validated");
                return Ok(true);
            }
            warn!(host = %host, age = %age, "Last handshake too old");
        }

        Ok(false)
    }

    async fn validate_control_plane_connectivity(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
        control_plane_ips: &[&str],
        api_server_url: &str,
    ) -> Result<()> {
        info!(host = %host, "Validating control plane connectivity");

        // Ping each control plane IP via WireGuard tunnel
        for ip in control_plane_ips {
            let ping_cmd = format!("ping -c 3 -W 5 {}", ip);
            match self
                .execute_ssh_command(host, port, ssh_config, &ping_cmd)
                .await
            {
                Ok(output) => {
                    if output.contains("3 received") || output.contains("3 packets transmitted") {
                        info!(host = %host, control_plane_ip = %ip, "Control plane ping successful");
                    } else {
                        return Err(AutoscalerError::NetworkValidation(format!(
                            "Ping to control plane {} failed: partial packet loss",
                            ip
                        )));
                    }
                }
                Err(e) => {
                    return Err(AutoscalerError::NetworkValidation(format!(
                        "Ping to control plane {} failed: {}",
                        ip, e
                    )));
                }
            }
        }

        // Check K3s API server connectivity
        let api_check_cmd = format!(
            "curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 10 -k {}/healthz",
            api_server_url
        );
        let status_code = self
            .execute_ssh_command(host, port, ssh_config, &api_check_cmd)
            .await?;

        let code = status_code.trim();
        if code != "200" && code != "401" && code != "403" {
            return Err(AutoscalerError::NetworkValidation(format!(
                "K3s API server connectivity check failed: HTTP {}",
                code
            )));
        }

        info!(host = %host, api_server = %api_server_url, "Control plane connectivity validated");
        Ok(())
    }

    async fn install_k3s_agent(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
        k3s_config: &K3sConfig,
        token: &str,
        node_id: &str,
    ) -> Result<String> {
        info!(host = %host, "Installing K3s agent");

        // Set hostname using safe slice (max 8 chars)
        let hostname = format!("gpu-{}", &node_id[..node_id.len().min(8)]);
        // Idempotent /etc/hosts update: only add if not already present
        let set_hostname = format!(
            "hostnamectl set-hostname {} && (grep -q '127.0.0.1 {}' /etc/hosts || echo '127.0.0.1 {}' >> /etc/hosts)",
            hostname, hostname, hostname
        );
        self.execute_ssh_command(host, port, ssh_config, &set_hostname)
            .await?;

        // Build K3s install command
        let server_url = &k3s_config.server_url;
        // Use wg0 as default flannel interface for WireGuard nodes
        let flannel_iface = "wg0";
        let node_labels = k3s_config
            .node_labels
            .iter()
            .map(|(k, v)| format!("--node-label={}={}", k, v))
            .collect::<Vec<_>>()
            .join(" ");

        // Include taint registration for unvalidated nodes
        let install_cmd = format!(
            r#"curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="agent" K3S_URL="{}" K3S_TOKEN="{}" sh -s - \
                --node-name={} \
                --flannel-iface={} \
                --node-label=basilica.ai/node-id={} \
                --node-label=basilica.ai/managed-by=autoscaler \
                --kubelet-arg=register-with-taints=basilica.ai/unvalidated=true:NoSchedule \
                {}"#,
            server_url, token, hostname, flannel_iface, node_id, node_labels
        );

        self.execute_ssh_command(host, port, ssh_config, &install_cmd)
            .await?;

        // Wait for K3s agent to start
        tokio::time::sleep(Duration::from_secs(10)).await;

        // Verify K3s agent is running
        let status = self
            .execute_ssh_command(
                host,
                port,
                ssh_config,
                "systemctl is-active k3s-agent || systemctl is-active k3s",
            )
            .await?;

        if !verify_k3s_status(&status) {
            return Err(AutoscalerError::K3sInstall(
                "K3s agent failed to start".to_string(),
            ));
        }

        // Log installed K3s version
        if let Ok(version_output) = self
            .execute_ssh_command(host, port, ssh_config, "k3s --version 2>/dev/null || true")
            .await
        {
            if let Some(version) = parse_k3s_version(&version_output) {
                info!(host = %host, version = %version, "K3s agent version");
            }
        }

        // Verify flannel.1 interface is UP (poll up to 30 attempts, 2s interval)
        self.verify_flannel_interface(host, port, ssh_config, &hostname)
            .await?;

        // Validate MTU settings
        self.validate_mtu_settings(host, port, ssh_config).await?;

        info!(host = %host, node_name = %hostname, "K3s agent installed and verified");
        Ok(hostname)
    }

    async fn execute_lifecycle_script(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
        script: &str,
        script_type: &str,
    ) -> Result<()> {
        if script.is_empty() {
            return Ok(());
        }

        info!(host = %host, script_type = %script_type, "Executing lifecycle script");

        // Execute the script with proper error handling
        let result = self
            .execute_ssh_command(host, port, ssh_config, script)
            .await;

        match result {
            Ok(output) => {
                debug!(host = %host, script_type = %script_type, output = %output, "Lifecycle script completed");
                Ok(())
            }
            Err(e) => {
                error!(host = %host, script_type = %script_type, error = %e, "Lifecycle script failed");
                Err(e)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ssh_provisioner_creation() {
        let provisioner = SshProvisioner::new(
            Duration::from_secs(30),
            Duration::from_secs(300),
            3,
            Duration::from_secs(5),
        );
        assert_eq!(provisioner.max_retries, 3);
    }
}
