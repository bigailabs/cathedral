use std::sync::Arc;
use std::time::Duration;

use tracing::{debug, warn};

use crate::api::{SecureCloudApi, WireGuardPeer};
use crate::controllers::AutoscalerK8sClient;
use crate::crd::NodePool;
use crate::error::Result;

/// WireGuard peer reconciler for server-side peer list updates
pub struct WireGuardInstaller<K, A>
where
    K: AutoscalerK8sClient,
    A: SecureCloudApi,
{
    k8s: Arc<K>,
    api: Arc<A>,
    reconcile_interval: Duration,
}

impl<K, A> WireGuardInstaller<K, A>
where
    K: AutoscalerK8sClient,
    A: SecureCloudApi,
{
    pub fn new(k8s: Arc<K>, api: Arc<A>, reconcile_interval: Duration) -> Self {
        Self {
            k8s,
            api,
            reconcile_interval,
        }
    }

    /// Reconcile WireGuard peers for all ready node pools
    pub async fn reconcile_all(&self, ns: &str) -> Result<()> {
        let node_pools = self.k8s.list_node_pools(ns).await?;

        for pool in node_pools {
            if let Some(status) = &pool.status {
                if status.phase == Some(crate::crd::NodePoolPhase::Ready) {
                    if let Err(e) = self.reconcile_pool(&pool).await {
                        warn!(pool = %pool.metadata.name.as_deref().unwrap_or("unknown"),
                              error = %e, "Failed to reconcile WireGuard peers");
                    }
                }
            }
        }

        Ok(())
    }

    async fn reconcile_pool(&self, pool: &NodePool) -> Result<()> {
        let name = pool.metadata.name.as_deref().unwrap_or("unknown");

        // node_id is in spec, not status
        let node_id = pool.spec.node_id.as_ref().ok_or_else(|| {
            crate::error::AutoscalerError::InvalidConfiguration("Missing node_id".to_string())
        })?;

        // Get current peer list from API
        let peers = self.api.get_peers(node_id).await?;

        // Check if peer list has changed (compare with stored peers)
        debug!(pool = %name, peer_count = %peers.len(), "Fetched current peers");

        // For now, we log the peer status
        // In a full implementation, we would SSH to the node and update peers
        // This is handled by the provisioner during setup, and this reconciler
        // runs periodically to detect any changes

        if peers.is_empty() {
            warn!(pool = %name, "No peers found for node");
        }

        Ok(())
    }

    pub fn interval(&self) -> Duration {
        self.reconcile_interval
    }
}

/// Generate WireGuard configuration file content
pub fn generate_wg_config(
    private_key: &str,
    address: &str,
    listen_port: u16,
    _interface_name: &str,
    peers: &[WireGuardPeer],
) -> String {
    // Generate config without PostUp/PostDown - iptables rules are handled
    // once during install_wireguard() to avoid duplicate rules on restart
    let mut config = format!(
        r#"[Interface]
PrivateKey = {}
Address = {}/16
ListenPort = {}
MTU = 1420
"#,
        private_key, address, listen_port
    );

    for peer in peers {
        config.push_str(&format!(
            r#"
[Peer]
PublicKey = {}
AllowedIPs = {}
Endpoint = {}
PersistentKeepalive = 25
"#,
            peer.public_key,
            peer.allowed_ips(),
            peer.endpoint
        ));
    }

    config
}

/// Parse WireGuard show output to extract peer information
pub fn parse_wg_show_output(output: &str) -> Vec<WireGuardPeerStatus> {
    let mut peers = Vec::new();
    let mut current_peer: Option<WireGuardPeerStatus> = None;

    for line in output.lines() {
        let line = line.trim();

        if line.starts_with("peer:") {
            if let Some(peer) = current_peer.take() {
                peers.push(peer);
            }
            let public_key = line.strip_prefix("peer:").unwrap().trim().to_string();
            current_peer = Some(WireGuardPeerStatus {
                public_key,
                endpoint: None,
                latest_handshake: None,
            });
        } else if let Some(ref mut peer) = current_peer {
            if line.starts_with("endpoint:") {
                peer.endpoint = Some(line.strip_prefix("endpoint:").unwrap().trim().to_string());
            } else if line.starts_with("latest handshake:") {
                peer.latest_handshake = Some(
                    line.strip_prefix("latest handshake:")
                        .unwrap()
                        .trim()
                        .to_string(),
                );
            }
        }
    }

    if let Some(peer) = current_peer {
        peers.push(peer);
    }

    peers
}

#[derive(Debug, Clone)]
pub struct WireGuardPeerStatus {
    #[allow(dead_code)] // Used in tests
    pub public_key: String,
    pub endpoint: Option<String>,
    pub latest_handshake: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generate_config_works() {
        let config = generate_wg_config(
            "test_private_key",
            "10.200.0.5",
            51820,
            "wg0",
            &[WireGuardPeer {
                public_key: "peer_pubkey".to_string(),
                endpoint: "1.2.3.4:51820".to_string(),
                wireguard_ip: "10.200.0.1".to_string(),
                vpc_subnet: "10.200.0.0/24".to_string(),
                route_pod_network: false,
            }],
        );

        assert!(config.contains("test_private_key"));
        assert!(config.contains("10.200.0.5/16"));
        assert!(config.contains("MTU = 1420"));
        assert!(config.contains("peer_pubkey"));
        assert!(config.contains("10.42.0.0/16")); // Pod network always routed
    }

    #[test]
    fn parse_wg_show_empty() {
        let peers = parse_wg_show_output("");
        assert!(peers.is_empty());
    }

    #[test]
    fn parse_wg_show_with_peer() {
        let output = r#"interface: wg0
  public key: local_pubkey
  private key: (hidden)
  listening port: 51820

peer: remote_pubkey
  endpoint: 1.2.3.4:51820
  allowed ips: 10.200.0.0/24
  latest handshake: 1 minute, 23 seconds ago
  transfer: 1.23 MiB received, 4.56 MiB sent
"#;
        let peers = parse_wg_show_output(output);
        assert_eq!(peers.len(), 1);
        assert_eq!(peers[0].public_key, "remote_pubkey");
        assert_eq!(peers[0].endpoint, Some("1.2.3.4:51820".to_string()));
    }
}
