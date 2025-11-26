//! WireGuard configuration types

use serde::{Deserialize, Serialize};

/// Individual K3s server WireGuard peer configuration
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WireGuardPeer {
    /// Server endpoint (public_ip:port)
    pub endpoint: String,

    /// Server's WireGuard public key
    pub public_key: String,

    /// Server's WireGuard IP (e.g., 10.200.0.1)
    pub wireguard_ip: String,

    /// Server's VPC subnet (e.g., 10.101.0.0/24) for routing
    pub vpc_subnet: String,

    /// Whether to route Flannel pod network through this peer
    pub route_pod_network: bool,
}

/// WireGuard configuration returned to GPU nodes during registration
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WireGuardConfig {
    /// Whether WireGuard is enabled for this node
    pub enabled: bool,

    /// IP assigned to this node on the WireGuard network (10.200.x.y)
    pub node_ip: String,

    /// All K3s server peers for multi-path routing
    pub peers: Vec<WireGuardPeer>,

    /// Persistent keepalive interval in seconds (keeps NAT mappings alive)
    pub persistent_keepalive: u32,
}

/// Legacy fields for backward compatibility with existing onboard.sh
impl WireGuardConfig {
    /// Primary server endpoint (first peer)
    pub fn server_endpoint(&self) -> String {
        self.peers.first().map(|p| p.endpoint.clone()).unwrap_or_default()
    }

    /// Primary server public key (first peer)
    pub fn server_public_key(&self) -> String {
        self.peers.first().map(|p| p.public_key.clone()).unwrap_or_default()
    }

    /// All allowed IPs (union of all peer routes)
    pub fn allowed_ips(&self) -> Vec<String> {
        let mut ips = Vec::new();
        for peer in &self.peers {
            ips.push(format!("{}/32", peer.wireguard_ip));
            ips.push(peer.vpc_subnet.clone());
            if peer.route_pod_network {
                ips.push("10.42.0.0/16".to_string());
            }
        }
        ips
    }
}

impl Default for WireGuardConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            node_ip: String::new(),
            peers: Vec::new(),
            persistent_keepalive: 25,
        }
    }
}

/// Individual server configuration from environment
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WireGuardServerEntry {
    /// Server endpoint (public_ip:port)
    pub endpoint: String,

    /// Server's WireGuard public key (base64 encoded)
    pub public_key: String,

    /// Server's WireGuard IP (e.g., 10.200.0.1)
    pub wireguard_ip: String,

    /// Server's VPC subnet (e.g., 10.101.0.0/24)
    pub vpc_subnet: String,
}

/// Server-side WireGuard configuration loaded from environment/config
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WireGuardServerConfig {
    /// Whether WireGuard is enabled for the cluster
    #[serde(default)]
    pub enabled: bool,

    /// All K3s servers with their WireGuard configurations
    #[serde(default)]
    pub servers: Vec<WireGuardServerEntry>,

    /// Persistent keepalive interval in seconds
    #[serde(default = "default_keepalive")]
    pub persistent_keepalive: u32,
}

fn default_keepalive() -> u32 {
    25
}

impl Default for WireGuardServerConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            servers: Vec::new(),
            persistent_keepalive: default_keepalive(),
        }
    }
}

impl WireGuardServerConfig {
    /// Create a WireGuard configuration for a specific node
    pub fn config_for_node(&self, node_ip: &str) -> WireGuardConfig {
        let peers: Vec<WireGuardPeer> = self
            .servers
            .iter()
            .enumerate()
            .map(|(i, server)| WireGuardPeer {
                endpoint: server.endpoint.clone(),
                public_key: server.public_key.clone(),
                wireguard_ip: server.wireguard_ip.clone(),
                vpc_subnet: server.vpc_subnet.clone(),
                route_pod_network: i == 0, // First server routes pod network
            })
            .collect();

        WireGuardConfig {
            enabled: self.enabled,
            node_ip: node_ip.to_string(),
            peers,
            persistent_keepalive: self.persistent_keepalive,
        }
    }

    /// Check if WireGuard is properly configured
    pub fn is_configured(&self) -> bool {
        self.enabled && !self.servers.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_wireguard_servers_from_json_env() {
        // Test that servers array can be parsed from JSON string (as set by Terraform)
        let json_str = r#"[
            {"endpoint":"1.2.3.4:51820","public_key":"key1","wireguard_ip":"10.200.0.1","vpc_subnet":"10.101.0.0/24"},
            {"endpoint":"1.2.3.5:51820","public_key":"key2","wireguard_ip":"10.200.0.2","vpc_subnet":"10.101.1.0/24"}
        ]"#;
        let parsed: Vec<WireGuardServerEntry> = serde_json::from_str(json_str).unwrap();
        assert_eq!(parsed.len(), 2);
        assert_eq!(parsed[0].endpoint, "1.2.3.4:51820");
        assert_eq!(parsed[0].vpc_subnet, "10.101.0.0/24");
        assert_eq!(parsed[1].wireguard_ip, "10.200.0.2");
    }

    #[test]
    fn test_wireguard_config_default() {
        let config = WireGuardConfig::default();
        assert!(!config.enabled);
        assert!(config.node_ip.is_empty());
        assert!(config.peers.is_empty());
        assert_eq!(config.persistent_keepalive, 25);
    }

    #[test]
    fn test_wireguard_server_config_is_configured() {
        let mut config = WireGuardServerConfig::default();
        assert!(!config.is_configured());

        config.enabled = true;
        assert!(!config.is_configured());

        config.servers.push(WireGuardServerEntry {
            endpoint: "1.2.3.4:51820".to_string(),
            public_key: "testkey".to_string(),
            wireguard_ip: "10.200.0.1".to_string(),
            vpc_subnet: "10.101.0.0/24".to_string(),
        });
        assert!(config.is_configured());
    }

    #[test]
    fn test_config_for_node_multi_server() {
        let server_config = WireGuardServerConfig {
            enabled: true,
            servers: vec![
                WireGuardServerEntry {
                    endpoint: "1.2.3.4:51820".to_string(),
                    public_key: "key1".to_string(),
                    wireguard_ip: "10.200.0.1".to_string(),
                    vpc_subnet: "10.101.0.0/24".to_string(),
                },
                WireGuardServerEntry {
                    endpoint: "1.2.3.5:51820".to_string(),
                    public_key: "key2".to_string(),
                    wireguard_ip: "10.200.0.2".to_string(),
                    vpc_subnet: "10.101.1.0/24".to_string(),
                },
                WireGuardServerEntry {
                    endpoint: "1.2.3.6:51820".to_string(),
                    public_key: "key3".to_string(),
                    wireguard_ip: "10.200.0.3".to_string(),
                    vpc_subnet: "10.101.2.0/24".to_string(),
                },
            ],
            persistent_keepalive: 30,
        };

        let node_config = server_config.config_for_node("10.200.42.1");
        assert!(node_config.enabled);
        assert_eq!(node_config.node_ip, "10.200.42.1");
        assert_eq!(node_config.peers.len(), 3);
        assert_eq!(node_config.persistent_keepalive, 30);

        // First peer routes pod network
        assert!(node_config.peers[0].route_pod_network);
        assert!(!node_config.peers[1].route_pod_network);
        assert!(!node_config.peers[2].route_pod_network);

        // Check VPC subnets
        assert_eq!(node_config.peers[0].vpc_subnet, "10.101.0.0/24");
        assert_eq!(node_config.peers[1].vpc_subnet, "10.101.1.0/24");
        assert_eq!(node_config.peers[2].vpc_subnet, "10.101.2.0/24");

        // Check allowed_ips helper
        let allowed = node_config.allowed_ips();
        assert!(allowed.contains(&"10.200.0.1/32".to_string()));
        assert!(allowed.contains(&"10.101.0.0/24".to_string()));
        assert!(allowed.contains(&"10.42.0.0/16".to_string())); // Pod network via first peer
        assert!(allowed.contains(&"10.200.0.2/32".to_string()));
        assert!(allowed.contains(&"10.101.1.0/24".to_string()));
    }

    #[test]
    fn test_legacy_accessors() {
        let server_config = WireGuardServerConfig {
            enabled: true,
            servers: vec![WireGuardServerEntry {
                endpoint: "1.2.3.4:51820".to_string(),
                public_key: "testkey".to_string(),
                wireguard_ip: "10.200.0.1".to_string(),
                vpc_subnet: "10.101.0.0/24".to_string(),
            }],
            persistent_keepalive: 25,
        };

        let node_config = server_config.config_for_node("10.200.42.1");
        assert_eq!(node_config.server_endpoint(), "1.2.3.4:51820");
        assert_eq!(node_config.server_public_key(), "testkey");
    }
}
