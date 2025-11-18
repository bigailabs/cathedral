use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct K3sServer {
    pub host: String,
    pub port: u16,
}

impl K3sServer {
    pub fn new(host: String, port: u16) -> Self {
        Self { host, port }
    }

    pub fn from_string(s: &str) -> Result<Self, String> {
        let parts: Vec<&str> = s.split(':').collect();
        match parts.len() {
            1 => Ok(Self::new(parts[0].to_string(), 22)),
            2 => {
                let port = parts[1]
                    .parse::<u16>()
                    .map_err(|e| format!("Invalid port: {}", e))?;
                Ok(Self::new(parts[0].to_string(), port))
            }
            _ => Err(format!("Invalid server format: {}", s)),
        }
    }

    pub fn to_string(&self) -> String {
        if self.port == 22 {
            self.host.clone()
        } else {
            format!("{}:{}", self.host, self.port)
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TokenResponse {
    pub token: String,
    pub token_id: String,
    pub node_password: Option<String>,
}

impl TokenResponse {
    pub fn new(token: String, token_id: String) -> Self {
        Self {
            token,
            token_id,
            node_password: None,
        }
    }

    pub fn with_node_password(token: String, token_id: String, node_password: String) -> Self {
        Self {
            token,
            token_id,
            node_password: Some(node_password),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_k3s_server_from_string() {
        let server = K3sServer::from_string("10.101.0.10").unwrap();
        assert_eq!(server.host, "10.101.0.10");
        assert_eq!(server.port, 22);

        let server = K3sServer::from_string("10.101.0.10:2222").unwrap();
        assert_eq!(server.host, "10.101.0.10");
        assert_eq!(server.port, 2222);

        assert!(K3sServer::from_string("10.101.0.10:invalid").is_err());
        assert!(K3sServer::from_string("10.101.0.10:22:extra").is_err());
    }
}
