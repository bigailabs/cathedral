use async_trait::async_trait;
use base64::prelude::*;
use std::io::Write;
use std::process::Stdio;
use std::time::Duration;
use tempfile::NamedTempFile;
use tokio::process::Command;
use tracing::{debug, error, info, warn};

use crate::api::WireGuardPeer;
use crate::crd::{K3sConfig, WireGuardConfig};
use crate::error::{AutoscalerError, Result};
use crate::provisioner::k3s::{parse_k3s_version, verify_k3s_status};
use crate::provisioner::wireguard::{generate_wg_config, parse_wg_show_output};

/// SSH connection configuration for system ssh command.
/// Note: Passphrase-protected keys are not supported as system ssh
/// requires either an unencrypted key or ssh-agent.
#[derive(Clone)]
pub struct SshConnectionConfig {
    pub username: String,
    pub private_key: String,
}

/// Result from K3s agent installation containing node info and GPU versions
#[derive(Clone, Debug)]
pub struct K3sJoinResult {
    /// Kubernetes node name (hostname)
    pub node_name: String,
    /// CUDA version detected on the node (e.g., "12.4")
    pub cuda_version: Option<String>,
    /// NVIDIA driver version detected on the node (e.g., "550.54.14")
    pub driver_version: Option<String>,
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

    /// Configure WireGuard peers with API-assigned node IP
    async fn configure_wireguard_peers(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
        peers: &[WireGuardPeer],
        node_ip: &str,
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
    ) -> Result<K3sJoinResult>;

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

/// Create a temporary file containing the private key for SSH -i option.
/// Returns the temp file handle (must be kept alive during SSH execution).
fn create_key_file(private_key: &str) -> Result<NamedTempFile> {
    let mut key_file = NamedTempFile::new().map_err(|e| AutoscalerError::SshConnection {
        host: "local".to_string(),
        reason: format!("Failed to create temp key file: {}", e),
    })?;

    key_file
        .write_all(private_key.as_bytes())
        .map_err(|e| AutoscalerError::SshConnection {
            host: "local".to_string(),
            reason: format!("Failed to write key file: {}", e),
        })?;

    // Ensure key has correct permissions (SSH requires 600)
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(key_file.path(), std::fs::Permissions::from_mode(0o600)).map_err(
            |e| AutoscalerError::SshConnection {
                host: "local".to_string(),
                reason: format!("Failed to set key permissions: {}", e),
            },
        )?;
    }

    Ok(key_file)
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

    /// Execute SSH command using system ssh binary.
    /// Uses OpenSSH with options designed to prevent SIGALRM (exit code 142)
    /// from server-side timeout mechanisms (PAM, sshd LoginGraceTime, etc).
    async fn execute_ssh_command(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
        command: &str,
    ) -> Result<String> {
        let mut last_error = None;

        // Escape single quotes in command for safe embedding in bash -c '...'
        let escaped_command = command.replace('\'', "'\\''");

        // Multi-layer signal isolation to prevent SIGALRM (exit 142):
        // 1. nohup: Ignores SIGHUP and makes process immune to hangup signals
        // 2. setsid: Creates new session, detaching from controlling terminal
        // 3. trap: Explicitly ignores ALRM, HUP, PIPE signals in the shell
        // 4. unset TMOUT: Disables bash's built-in timeout
        // 5. alarm 0: Cancels any pending alarm in the shell process
        // 6. </dev/null: Prevents stdin reads that could trigger timeouts
        // 7. 2>&1: Captures stderr in stdout for better error diagnostics
        let wrapped_command = format!(
            r#"nohup setsid bash --norc --noprofile -c '
trap "" ALRM HUP PIPE TERM
unset TMOUT
perl -e "alarm(0)" 2>/dev/null || true
{}
' </dev/null 2>&1"#,
            escaped_command
        );

        for attempt in 0..self.max_retries {
            if attempt > 0 {
                debug!(host = %host, attempt = %attempt, "Retrying SSH connection");
                tokio::time::sleep(self.retry_delay).await;
            }

            // Create temp file for private key (must stay alive during command execution)
            let key_file = create_key_file(&ssh_config.private_key)?;

            let mut cmd = Command::new("ssh");
            cmd
                // Redirect stdin from /dev/null - prevents ssh from reading stdin
                // which can interfere with timeout handling on some systems
                .arg("-n")
                // Force no TTY allocation - avoids PTY-related timeouts
                // Some PAM modules only set alarms for PTY sessions
                .arg("-T")
                // Private key file
                .arg("-i")
                .arg(key_file.path())
                // Port
                .arg("-p")
                .arg(port.to_string())
                // Disable strict host key checking (VMs have dynamic IPs)
                .arg("-o")
                .arg("StrictHostKeyChecking=no")
                .arg("-o")
                .arg("UserKnownHostsFile=/dev/null")
                // Log level quiet to reduce noise
                .arg("-o")
                .arg("LogLevel=ERROR")
                // Essential for non-interactive automation
                .arg("-o")
                .arg("BatchMode=yes")
                .arg("-o")
                .arg("IdentitiesOnly=yes")
                // Disable PTY request explicitly
                .arg("-o")
                .arg("RequestTTY=no")
                // Connection timeout
                .arg("-o")
                .arg(format!(
                    "ConnectTimeout={}",
                    self.connection_timeout.as_secs()
                ))
                // Keepalive: send every 30s, close after 4 failures (2min of no response)
                .arg("-o")
                .arg("ServerAliveInterval=30")
                .arg("-o")
                .arg("ServerAliveCountMax=4")
                // Disable any local command execution hooks
                .arg("-o")
                .arg("PermitLocalCommand=no")
                // User and host
                .arg(format!("{}@{}", ssh_config.username, host))
                .arg(&wrapped_command);

            cmd.stdout(Stdio::piped());
            cmd.stderr(Stdio::piped());

            // Set TERM to dumb to avoid terminal-specific behaviors
            cmd.env("TERM", "dumb");

            debug!(host = %host, command = %command, "Executing SSH command");

            let result = tokio::time::timeout(self.execution_timeout, cmd.output()).await;

            match result {
                Ok(Ok(output)) => {
                    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
                    let stderr = String::from_utf8_lossy(&output.stderr).to_string();

                    if output.status.success() {
                        debug!(host = %host, "SSH command executed successfully");
                        return Ok(stdout);
                    }

                    let exit_code = output.status.code().unwrap_or(255) as u32;

                    // Exit code 142 = 128 + 14 (SIGALRM)
                    // If we still get SIGALRM, log detailed diagnostics
                    if exit_code == 142 {
                        error!(
                            host = %host,
                            exit_code = %exit_code,
                            stdout = %stdout,
                            stderr = %stderr,
                            command = %command,
                            "SSH command killed by SIGALRM - server-side timeout mechanism active"
                        );
                    } else {
                        warn!(
                            host = %host,
                            exit_code = %exit_code,
                            stderr = %stderr,
                            "SSH command failed"
                        );
                    }

                    last_error = Some(AutoscalerError::SshExecution {
                        command: command.to_string(),
                        exit_code,
                        stderr,
                    });
                }
                Ok(Err(e)) => {
                    warn!(host = %host, error = %e, "Failed to spawn SSH process");
                    last_error = Some(AutoscalerError::SshConnection {
                        host: host.to_string(),
                        reason: format!("Failed to spawn ssh: {}", e),
                    });
                }
                Err(_) => {
                    warn!(host = %host, "SSH command execution timed out");
                    last_error = Some(AutoscalerError::SshConnection {
                        host: host.to_string(),
                        reason: format!(
                            "Command execution timed out after {}s",
                            self.execution_timeout.as_secs()
                        ),
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
        // Use sudo for writing to system paths (e.g., /etc/wireguard/)
        let encoded = base64::engine::general_purpose::STANDARD.encode(content);
        let command = format!(
            "echo '{}' | base64 -d | sudo tee {} > /dev/null && sudo chmod {} {}",
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

        // Wait for cloud-init to complete and dpkg lock to be released.
        // Fresh Ubuntu cloud images run unattended-upgrades which holds
        // the dpkg lock for 1-2 minutes after boot.
        // Note: All commands use sudo as ubuntu user doesn't have root privileges.
        let script = r#"
export DEBIAN_FRONTEND=noninteractive

# Wait for cloud-init to complete (max 120s)
cloud-init status --wait 2>/dev/null || true

# Wait for dpkg lock to be released (max 120s)
for i in $(seq 1 60); do
    if ! sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 && \
       ! sudo fuser /var/lib/apt/lists/lock >/dev/null 2>&1; then
        break
    fi
    echo "Waiting for dpkg lock... ($i/60)"
    sleep 2
done

# Kill any stale apt/dpkg processes and clean up locks if still held
sudo killall -9 apt apt-get dpkg 2>/dev/null || true
sudo rm -f /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock 2>/dev/null || true
sudo dpkg --configure -a 2>/dev/null || true

sudo apt-get update -qq && \
sudo apt-get install -y -qq curl wget gnupg2 ca-certificates \
    lsb-release apt-transport-https software-properties-common

# Install NVIDIA Container Toolkit if nvidia-smi is available but nvidia-ctk is not
if command -v nvidia-smi &> /dev/null && ! command -v nvidia-ctk &> /dev/null; then
    echo "Installing NVIDIA Container Toolkit..."
    distribution=$(. /etc/os-release;echo $ID$VERSION_ID) && \
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg && \
    curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
        sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
        sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list && \
    sudo apt-get update -qq && \
    sudo apt-get install -y -qq nvidia-container-toolkit
fi

# Configure containerd for NVIDIA runtime (creates drop-in config for K3s)
# This creates /etc/containerd/conf.d/99-nvidia.toml which K3s containerd reads.
# Run unconditionally to ensure config exists before K3s starts.
if command -v nvidia-ctk &> /dev/null; then
    echo "Configuring containerd for NVIDIA runtime..."
    sudo nvidia-ctk runtime configure --runtime=containerd
    # Restart containerd only if it's running (standalone containerd)
    if systemctl is-active --quiet containerd 2>/dev/null; then
        sudo systemctl restart containerd
    fi
fi
"#;

        self.execute_ssh_command(host, port, ssh_config, script)
            .await?;

        // Apply network performance tuning (matching onboard.sh setup_performance_tuning)
        info!(host = %host, "Applying network performance tuning");
        let sysctl_tuning = r#"
# Load kernel modules for performance tuning
sudo modprobe tcp_bbr 2>/dev/null && echo "tcp_bbr" | sudo tee /etc/modules-load.d/bbr.conf || true
sudo modprobe nf_conntrack 2>/dev/null && echo "nf_conntrack" | sudo tee /etc/modules-load.d/conntrack.conf || true
sudo modprobe br_netfilter 2>/dev/null && echo "br_netfilter" | sudo tee /etc/modules-load.d/br_netfilter.conf || true

# Deploy sysctl performance configuration
sudo tee /etc/sysctl.d/99-wireguard-performance.conf > /dev/null <<'SYSCTL_EOF'
# WireGuard and Network Performance Tuning for K3s GPU Clusters
# Deployed by Basilica autoscaler - Do not edit manually
# Architecture: WireGuard (MTU 1420) -> Flannel VXLAN (MTU ~1370) -> Pods

# IP forwarding and routing (mandatory for K3s/Flannel)
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
net.ipv4.conf.all.rp_filter = 2
net.ipv4.conf.default.rp_filter = 2

# Bridge netfilter (mandatory for Flannel/kube-proxy)
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1

# Socket buffer sizing (64MB max for high-throughput GPU workloads)
net.core.rmem_max = 67108864
net.core.wmem_max = 67108864
net.core.rmem_default = 16777216
net.core.wmem_default = 16777216
net.ipv4.tcp_rmem = 4096 1048576 67108864
net.ipv4.tcp_wmem = 4096 1048576 67108864
net.ipv4.udp_rmem_min = 16384
net.ipv4.udp_wmem_min = 16384

# Network device tuning (increased for 10Gbps line-rate)
net.core.netdev_max_backlog = 50000
net.core.netdev_budget = 3000
net.core.netdev_budget_usecs = 8000
net.core.somaxconn = 65535

# BBR congestion control (ideal for WireGuard tunnels)
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.ipv4.tcp_notsent_lowat = 16384

# Connection tracking (1M entries, tuned timeouts for K8s)
net.netfilter.nf_conntrack_max = 1048576
net.netfilter.nf_conntrack_tcp_timeout_established = 7200
net.netfilter.nf_conntrack_tcp_timeout_time_wait = 30
net.netfilter.nf_conntrack_udp_timeout = 120
net.netfilter.nf_conntrack_udp_timeout_stream = 180

# TCP optimizations
net.ipv4.tcp_fastopen = 3
net.ipv4.tcp_max_orphans = 65536
net.ipv4.tcp_max_syn_backlog = 65536
net.ipv4.tcp_window_scaling = 1
net.ipv4.tcp_timestamps = 1
net.ipv4.tcp_sack = 1
net.ipv4.tcp_slow_start_after_idle = 0

# Path MTU Discovery (critical for nested encapsulation)
net.ipv4.ip_no_pmtu_disc = 0
net.ipv4.tcp_mtu_probing = 1
net.ipv4.tcp_base_mss = 1280

# ARP cache tuning (for large clusters)
net.ipv4.neigh.default.gc_thresh1 = 8192
net.ipv4.neigh.default.gc_thresh2 = 32768
net.ipv4.neigh.default.gc_thresh3 = 65536

# Inotify limits (for kubelet/containerd)
fs.inotify.max_user_instances = 8192
fs.inotify.max_user_watches = 524288

# File descriptor limits
fs.file-max = 2097152

# ICMP rate limiting (security + PMTUD)
net.ipv4.icmp_ratelimit = 1000
net.ipv4.icmp_msgs_per_sec = 1000
SYSCTL_EOF

sudo chmod 644 /etc/sysctl.d/99-wireguard-performance.conf
sudo sysctl --system > /dev/null 2>&1 || true
"#;
        self.execute_ssh_command(host, port, ssh_config, sysctl_tuning)
            .await?;

        info!(host = %host, "Base system configuration complete");
        Ok(())
    }

    async fn install_wireguard(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
        _node_ip: &str,
        _config: &WireGuardConfig,
    ) -> Result<String> {
        info!(host = %host, "Installing WireGuard");

        // Install WireGuard and ensure directory exists
        self.execute_ssh_command(
            host,
            port,
            ssh_config,
            "sudo apt-get install -y -qq wireguard wireguard-tools && sudo mkdir -p /etc/wireguard && sudo chmod 700 /etc/wireguard",
        )
        .await?;

        // Check for existing valid keypair (preserve like onboard.sh does)
        // Note: onboard.sh uses private.key (with dot), not privatekey
        let existing_key = self
            .execute_ssh_command(
                host,
                port,
                ssh_config,
                "sudo cat /etc/wireguard/private.key 2>/dev/null || true",
            )
            .await?;

        let (_private_key, public_key) = if !existing_key.trim().is_empty()
            && existing_key.trim().len() == 44
        {
            // Valid existing key found - preserve it and derive public key
            info!(host = %host, "Preserving existing WireGuard keypair");
            let pubkey = self
                .execute_ssh_command(
                    host,
                    port,
                    ssh_config,
                    &format!("echo '{}' | wg pubkey", existing_key.trim()),
                )
                .await?;
            (existing_key.trim().to_string(), pubkey.trim().to_string())
        } else {
            // Generate new keypair (like onboard.sh)
            info!(host = %host, "Generating new WireGuard keypair");
            let privkey = self
                .execute_ssh_command(host, port, ssh_config, "wg genkey")
                .await?;
            let pubkey = self
                .execute_ssh_command(
                    host,
                    port,
                    ssh_config,
                    &format!("echo '{}' | wg pubkey", privkey.trim()),
                )
                .await?;

            // Save keys to files (matching onboard.sh naming: private.key, public.key)
            self.execute_ssh_command(
                host,
                port,
                ssh_config,
                &format!(
                    "echo '{}' | sudo tee /etc/wireguard/private.key > /dev/null && sudo chmod 600 /etc/wireguard/private.key && echo '{}' | sudo tee /etc/wireguard/public.key > /dev/null && sudo chmod 644 /etc/wireguard/public.key",
                    privkey.trim(),
                    pubkey.trim()
                ),
            )
            .await?;

            (privkey.trim().to_string(), pubkey.trim().to_string())
        };

        info!(host = %host, "WireGuard installed and configured");
        Ok(public_key)
    }

    async fn configure_wireguard_peers(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
        peers: &[WireGuardPeer],
        node_ip: &str,
    ) -> Result<()> {
        info!(host = %host, peer_count = %peers.len(), node_ip = %node_ip, "Configuring WireGuard peers");

        // Read private key (using onboard.sh naming: private.key)
        let private_key = self
            .execute_ssh_command(
                host,
                port,
                ssh_config,
                "sudo cat /etc/wireguard/private.key",
            )
            .await?;

        // Check for existing wg0 with mismatched IP and tear down if necessary
        if let Ok(output) = self
            .execute_ssh_command(
                host,
                port,
                ssh_config,
                "ip addr show wg0 2>/dev/null | grep -oP 'inet \\K[0-9.]+' || true",
            )
            .await
        {
            let existing_ip = output.trim();
            if !existing_ip.is_empty() && existing_ip != node_ip {
                warn!(
                    host = %host,
                    existing_ip = %existing_ip,
                    expected_ip = %node_ip,
                    "Existing WireGuard IP mismatch detected, tearing down interface"
                );
                self.execute_ssh_command(
                    host,
                    port,
                    ssh_config,
                    "sudo systemctl stop wg-quick@wg0 2>/dev/null || true; sudo ip link del wg0 2>/dev/null || true",
                )
                .await?;
            }
        }

        // Generate WireGuard config with API-assigned IP
        let wg_config = generate_wg_config(private_key.trim(), node_ip, 51820, "wg0", peers);

        // Upload config
        self.upload_file(
            host,
            port,
            ssh_config,
            &wg_config,
            "/etc/wireguard/wg0.conf",
            "600",
        )
        .await?;

        // Enable and start WireGuard
        self.execute_ssh_command(
            host,
            port,
            ssh_config,
            "sudo systemctl enable wg-quick@wg0 && sudo systemctl restart wg-quick@wg0",
        )
        .await?;

        // Verify WireGuard is running
        let status = self
            .execute_ssh_command(host, port, ssh_config, "sudo wg show wg0")
            .await?;

        let peer_status = parse_wg_show_output(&status);
        info!(host = %host, active_peers = %peer_status.len(), "WireGuard peers configured");

        Ok(())
    }

    async fn validate_wireguard_connectivity(
        &self,
        host: &str,
        port: u16,
        ssh_config: &SshConnectionConfig,
    ) -> Result<bool> {
        debug!(host = %host, "Validating WireGuard connectivity");

        // Check if wg0 interface exists and is UP
        let output = self
            .execute_ssh_command(
                host,
                port,
                ssh_config,
                "ip link show wg0 2>/dev/null || true",
            )
            .await?;

        if !output.contains("state UP") && !output.contains("state UNKNOWN") {
            warn!(host = %host, "WireGuard interface wg0 is not UP");
            return Ok(false);
        }

        // Check for active peers
        let wg_output = self
            .execute_ssh_command(
                host,
                port,
                ssh_config,
                "sudo wg show wg0 2>/dev/null || true",
            )
            .await?;

        let peers = parse_wg_show_output(&wg_output);
        if peers.is_empty() {
            warn!(host = %host, "No WireGuard peers configured");
            return Ok(false);
        }

        // Check for recent handshakes (within last 2 minutes)
        let has_recent_handshake = peers.iter().any(|peer| {
            peer.latest_handshake
                .as_ref()
                .map(|hs| {
                    hs.contains("seconds ago")
                        || hs.contains("1 minute")
                        || hs.contains("Less than a minute")
                })
                .unwrap_or(false)
        });

        if !has_recent_handshake {
            warn!(host = %host, "No recent WireGuard handshakes detected");
            return Ok(false);
        }

        info!(host = %host, active_peers = %peers.len(), "WireGuard connectivity validated");
        Ok(true)
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

        // Ping each control plane IP
        for cp_ip in control_plane_ips {
            let ping_cmd = format!("ping -c 3 -W 2 {} || true", cp_ip);
            let output = self
                .execute_ssh_command(host, port, ssh_config, &ping_cmd)
                .await?;

            if !output.contains("3 received") && !output.contains("3 packets received") {
                return Err(AutoscalerError::ConnectivityCheck {
                    target: cp_ip.to_string(),
                    reason: "Ping failed".to_string(),
                });
            }
        }

        // Test K3s API server connectivity (extract host:port from URL)
        let api_host = api_server_url
            .trim_start_matches("https://")
            .trim_start_matches("http://");

        let nc_cmd = format!("nc -zv -w 5 {} 2>&1 || true", api_host.replace(':', " "));
        let output = self
            .execute_ssh_command(host, port, ssh_config, &nc_cmd)
            .await?;

        if !output.contains("succeeded") && !output.contains("open") {
            return Err(AutoscalerError::ConnectivityCheck {
                target: api_host.to_string(),
                reason: "K3s API server unreachable".to_string(),
            });
        }

        info!(host = %host, "Control plane connectivity validated");
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
    ) -> Result<K3sJoinResult> {
        info!(host = %host, "Installing K3s agent");

        // Get hostname for node registration
        let hostname = self
            .execute_ssh_command(host, port, ssh_config, "hostname -s")
            .await?
            .trim()
            .to_string();

        // Detect NVIDIA driver and CUDA versions for node labels
        let driver_version = self
            .execute_ssh_command(
                host,
                port,
                ssh_config,
                "nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1 | tr -d '[:space:]'",
            )
            .await
            .ok()
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| "unknown".to_string());

        let cuda_version = self
            .execute_ssh_command(
                host,
                port,
                ssh_config,
                "nvidia-smi | grep -oP 'CUDA Version: \\K[0-9.]+' | head -1 | tr -d '[:space:]'",
            )
            .await
            .ok()
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| "unknown".to_string());

        info!(host = %host, driver = %driver_version, cuda = %cuda_version, "Detected NVIDIA versions");

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
        // Add driver/cuda version labels for operator validation
        let install_cmd = format!(
            r#"curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="agent" K3S_URL="{}" K3S_TOKEN="{}" sh -s - \
                --node-name={} \
                --flannel-iface={} \
                --node-label=basilica.ai/node-id={} \
                --node-label=basilica.ai/managed-by=autoscaler \
                --node-label=basilica.ai/driver-version={} \
                --node-label=basilica.ai/cuda-version={} \
                --kubelet-arg=register-with-taints=basilica.ai/unvalidated=true:NoSchedule \
                {}"#,
            server_url,
            token,
            hostname,
            flannel_iface,
            node_id,
            driver_version,
            cuda_version,
            node_labels
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
                "sudo systemctl is-active k3s-agent || sudo systemctl is-active k3s",
            )
            .await?;

        if !verify_k3s_status(&status) {
            return Err(AutoscalerError::K3sInstall(
                "K3s agent failed to start".to_string(),
            ));
        }

        // Configure NVIDIA container runtime for K3s containerd
        // Uses base64 encoding to transfer the script, avoiding shell escaping issues
        // This must run AFTER K3s is installed since K3s creates its containerd config.
        let encoded_script = BASE64_STANDARD.encode(NVIDIA_CONFIGURE_SCRIPT);
        let nvidia_configure_cmd = format!("echo '{}' | base64 -d | sudo bash", encoded_script);
        if let Err(e) = self
            .execute_ssh_command(host, port, ssh_config, &nvidia_configure_cmd)
            .await
        {
            warn!(host = %host, error = %e, "Failed to configure NVIDIA runtime for containerd");
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

        // Return node info with detected versions
        let cuda = if cuda_version == "unknown" {
            None
        } else {
            Some(cuda_version)
        };
        let driver = if driver_version == "unknown" {
            None
        } else {
            Some(driver_version)
        };

        Ok(K3sJoinResult {
            node_name: hostname,
            cuda_version: cuda,
            driver_version: driver,
        })
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

/// NVIDIA container runtime configuration script for K3s containerd.
/// This script is transferred to the node via base64 encoding to avoid shell escaping issues.
const NVIDIA_CONFIGURE_SCRIPT: &str = r#"#!/bin/bash
set -e

CONFIG_DIR="/var/lib/rancher/k3s/agent/etc/containerd"
CONFIG_FILE="$CONFIG_DIR/config.toml"
TMPL_FILE="$CONFIG_DIR/config.toml.tmpl"

# Skip if no GPU or no K3s config
if [ ! -f "$CONFIG_FILE" ]; then
    echo "K3s containerd config not found, skipping NVIDIA configuration"
    exit 0
fi
if ! command -v nvidia-smi > /dev/null 2>&1; then
    echo "nvidia-smi not found, skipping NVIDIA configuration"
    exit 0
fi

# Check if nvidia is already set as default runtime (idempotency)
if sudo grep -q 'default_runtime_name.*=.*nvidia' "$CONFIG_FILE"; then
    echo "NVIDIA already set as default runtime in K3s containerd"
    exit 0
fi

echo "Configuring NVIDIA runtime for K3s containerd..."

# Create template from current config if not exists
if [ ! -f "$TMPL_FILE" ]; then
    sudo cp "$CONFIG_FILE" "$TMPL_FILE"
fi

# Detect config version
if sudo grep -q 'version = 3' "$TMPL_FILE"; then
    echo "Detected containerd v3 config"
    IS_V3=true
else
    echo "Detected containerd v2 config"
    IS_V3=false
fi

# For v3: Add containerd section with default_runtime_name before runtimes
# For v2: The section already exists, just add the line
if ! sudo grep -q 'default_runtime_name' "$TMPL_FILE"; then
    echo "Adding default_runtime_name = nvidia..."
    if [ "$IS_V3" = true ]; then
        # v3: Find line number for runtimes.runc and insert containerd section before it
        LINE=$(sudo grep -n 'containerd\.runtimes\.runc\]$' "$TMPL_FILE" | head -1 | cut -d: -f1)
        if [ -n "$LINE" ]; then
            sudo sed -i "${LINE}i\\[plugins.'io.containerd.cri.v1.runtime'.containerd]\\n  default_runtime_name = \"nvidia\"\\n" "$TMPL_FILE"
        else
            echo "Warning: Could not find runtimes.runc section"
        fi
    else
        # v2: Add after existing containerd section
        sudo sed -i '/\[plugins\."io\.containerd\.grpc\.v1\.cri"\.containerd\]/a\  default_runtime_name = "nvidia"' "$TMPL_FILE"
    fi
fi

# Add nvidia runtime if not present
if ! sudo grep -q "runtimes.*'nvidia'\|runtimes\.nvidia" "$TMPL_FILE"; then
    echo "Adding nvidia runtime configuration..."
    if [ "$IS_V3" = true ]; then
        # v3 format with single quotes
        cat << 'NVIDIA_V3' | sudo tee -a "$TMPL_FILE" > /dev/null

[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.'nvidia']
  runtime_type = "io.containerd.runc.v2"

[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.'nvidia'.options]
  BinaryName = "/usr/bin/nvidia-container-runtime"
  SystemdCgroup = true
NVIDIA_V3
    else
        # v2 format with double quotes
        cat << 'NVIDIA_V2' | sudo tee -a "$TMPL_FILE" > /dev/null

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia]
  runtime_type = "io.containerd.runc.v2"

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia.options]
  BinaryName = "/usr/bin/nvidia-container-runtime"
NVIDIA_V2
    fi
fi

# Restart K3s agent to regenerate config from template
echo "Restarting K3s agent to apply changes..."
sudo systemctl restart k3s-agent
sleep 5

# Verify nvidia is set as default runtime
if sudo grep -q 'default_runtime_name.*=.*nvidia' "$CONFIG_FILE"; then
    echo "NVIDIA successfully set as default runtime in K3s containerd"
else
    echo "Warning: default_runtime_name not set to nvidia after restart"
    sudo cat "$CONFIG_FILE"
    exit 1
fi
"#;

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
        assert_eq!(provisioner.connection_timeout, Duration::from_secs(30));
        assert_eq!(provisioner.execution_timeout, Duration::from_secs(300));
    }

    #[test]
    fn create_key_file_works() {
        let key_content = "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----";
        let key_file = create_key_file(key_content).unwrap();
        let content = std::fs::read_to_string(key_file.path()).unwrap();
        assert_eq!(content, key_content);
    }
}
