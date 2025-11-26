//! WireGuard configuration types

use serde::{Deserialize, Serialize};

/// WireGuard configuration returned to GPU nodes during registration
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WireGuardConfig {
    /// Whether WireGuard is enabled for this node
    pub enabled: bool,

    /// Server endpoint (public_ip:port) for the node to connect to
    pub server_endpoint: String,

    /// Server's WireGuard public key
    pub server_public_key: String,

    /// IP assigned to this node on the WireGuard network (10.200.x.y)
    pub node_ip: String,

    /// CIDR ranges allowed through the tunnel
    pub allowed_ips: Vec<String>,

    /// Persistent keepalive interval in seconds (keeps NAT mappings alive)
    pub persistent_keepalive: u32,
}

impl Default for WireGuardConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            server_endpoint: String::new(),
            server_public_key: String::new(),
            node_ip: String::new(),
            allowed_ips: vec![
                "10.200.0.0/16".to_string(), // WireGuard overlay network
                "10.42.0.0/16".to_string(),  // Flannel pod network
            ],
            persistent_keepalive: 25,
        }
    }
}

/// Server-side WireGuard configuration loaded from environment/config
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WireGuardServerConfig {
    /// Whether WireGuard is enabled for the cluster
    #[serde(default)]
    pub enabled: bool,

    /// Primary server endpoint (public_ip:port)
    #[serde(default)]
    pub server_endpoint: String,

    /// Server's WireGuard public key (base64 encoded)
    #[serde(default)]
    pub server_public_key: String,

    /// CIDR ranges to allow through the tunnel
    #[serde(default = "default_allowed_ips")]
    pub allowed_ips: Vec<String>,

    /// Persistent keepalive interval in seconds
    #[serde(default = "default_keepalive")]
    pub persistent_keepalive: u32,
}

fn default_allowed_ips() -> Vec<String> {
    vec!["10.200.0.0/16".to_string(), "10.42.0.0/16".to_string()]
}

fn default_keepalive() -> u32 {
    25
}

impl Default for WireGuardServerConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            server_endpoint: String::new(),
            server_public_key: String::new(),
            allowed_ips: default_allowed_ips(),
            persistent_keepalive: default_keepalive(),
        }
    }
}

impl WireGuardServerConfig {
    /// Create a WireGuard configuration for a specific node
    pub fn config_for_node(&self, node_ip: &str) -> WireGuardConfig {
        WireGuardConfig {
            enabled: self.enabled,
            server_endpoint: self.server_endpoint.clone(),
            server_public_key: self.server_public_key.clone(),
            node_ip: node_ip.to_string(),
            allowed_ips: self.allowed_ips.clone(),
            persistent_keepalive: self.persistent_keepalive,
        }
    }

    /// Check if WireGuard is properly configured
    pub fn is_configured(&self) -> bool {
        self.enabled && !self.server_endpoint.is_empty() && !self.server_public_key.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_wireguard_server_config_from_json_env() {
        // Test that allowed_ips can be parsed from JSON string (as set by Terraform)
        let json_str = r#"["10.200.0.0/16","10.42.0.0/16"]"#;
        let parsed: Vec<String> = serde_json::from_str(json_str).unwrap();
        assert_eq!(parsed.len(), 2);
        assert_eq!(parsed[0], "10.200.0.0/16");
        assert_eq!(parsed[1], "10.42.0.0/16");
    }

    #[test]
    fn test_wireguard_config_default() {
        let config = WireGuardConfig::default();
        assert!(!config.enabled);
        assert!(config.server_endpoint.is_empty());
        assert_eq!(config.persistent_keepalive, 25);
        assert_eq!(config.allowed_ips.len(), 2);
    }

    #[test]
    fn test_wireguard_server_config_is_configured() {
        let mut config = WireGuardServerConfig::default();
        assert!(!config.is_configured());

        config.enabled = true;
        assert!(!config.is_configured());

        config.server_endpoint = "1.2.3.4:51820".to_string();
        assert!(!config.is_configured());

        config.server_public_key = "base64pubkey".to_string();
        assert!(config.is_configured());
    }

    #[test]
    fn test_config_for_node() {
        let server_config = WireGuardServerConfig {
            enabled: true,
            server_endpoint: "1.2.3.4:51820".to_string(),
            server_public_key: "testkey123".to_string(),
            allowed_ips: vec!["10.200.0.0/16".to_string()],
            persistent_keepalive: 30,
        };

        let node_config = server_config.config_for_node("10.200.42.1");
        assert!(node_config.enabled);
        assert_eq!(node_config.node_ip, "10.200.42.1");
        assert_eq!(node_config.server_endpoint, "1.2.3.4:51820");
        assert_eq!(node_config.persistent_keepalive, 30);
    }
}
