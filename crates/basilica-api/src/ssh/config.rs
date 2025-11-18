use super::types::K3sServer;
use serde::{Deserialize, Deserializer, Serialize};

fn deserialize_servers<'de, D>(deserializer: D) -> Result<Vec<K3sServer>, D::Error>
where
    D: Deserializer<'de>,
{
    let s: String = String::deserialize(deserializer)?;
    if s.is_empty() {
        return Ok(vec![]);
    }
    s.split(',')
        .map(|server_str| {
            K3sServer::from_string(server_str.trim()).map_err(serde::de::Error::custom)
        })
        .collect()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct K3sSshConfig {
    pub enabled: bool,
    #[serde(deserialize_with = "deserialize_servers")]
    pub servers: Vec<K3sServer>,
    pub ssh_key_path: String,
    pub known_hosts_path: Option<String>,
    pub username: String,
    pub timeout_secs: u64,
}

impl Default for K3sSshConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            servers: vec![],
            ssh_key_path: "/tmp/.ssh/k3s_key".to_string(),
            known_hosts_path: Some("/etc/ssh/known_hosts".to_string()),
            username: "ubuntu".to_string(),
            timeout_secs: 30,
        }
    }
}
