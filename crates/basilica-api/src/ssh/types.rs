use serde::{Deserialize, Serialize};
use std::fmt;

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
        if s.is_empty() {
            return Err("Empty server string".to_string());
        }

        if let Some(s) = s.strip_prefix('[') {
            if let Some((host, port_str)) = s.split_once("]:") {
                let port = port_str
                    .parse::<u16>()
                    .map_err(|e| format!("Invalid port: {}", e))?;
                return Ok(Self::new(host.to_string(), port));
            }
            return Err(format!("Invalid bracketed IPv6 format: {}", s));
        }

        match s.rsplit_once(':') {
            Some((host, port_str)) => {
                if host.is_empty() || host.contains(':') {
                    Ok(Self::new(s.to_string(), 22))
                } else {
                    let port = port_str
                        .parse::<u16>()
                        .map_err(|e| format!("Invalid port: {}", e))?;
                    Ok(Self::new(host.to_string(), port))
                }
            }
            None => Ok(Self::new(s.to_string(), 22)),
        }
    }
}

impl fmt::Display for K3sServer {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let is_ipv6 = self.host.contains(':');

        if self.port == 22 {
            write!(f, "{}", self.host)
        } else if is_ipv6 {
            write!(f, "[{}]:{}", self.host, self.port)
        } else {
            write!(f, "{}:{}", self.host, self.port)
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
    fn test_k3s_server_from_string_ipv4() {
        let server = K3sServer::from_string("10.101.0.10").unwrap();
        assert_eq!(server.host, "10.101.0.10");
        assert_eq!(server.port, 22);

        let server = K3sServer::from_string("10.101.0.10:2222").unwrap();
        assert_eq!(server.host, "10.101.0.10");
        assert_eq!(server.port, 2222);

        let server = K3sServer::from_string("192.168.1.1:8080").unwrap();
        assert_eq!(server.host, "192.168.1.1");
        assert_eq!(server.port, 8080);
    }

    #[test]
    fn test_k3s_server_from_string_hostname() {
        let server = K3sServer::from_string("example.com").unwrap();
        assert_eq!(server.host, "example.com");
        assert_eq!(server.port, 22);

        let server = K3sServer::from_string("example.com:8080").unwrap();
        assert_eq!(server.host, "example.com");
        assert_eq!(server.port, 8080);

        let server = K3sServer::from_string("k3s-server.local:6443").unwrap();
        assert_eq!(server.host, "k3s-server.local");
        assert_eq!(server.port, 6443);
    }

    #[test]
    fn test_k3s_server_from_string_bracketed_ipv6_with_port() {
        let server = K3sServer::from_string("[::1]:22").unwrap();
        assert_eq!(server.host, "::1");
        assert_eq!(server.port, 22);

        let server = K3sServer::from_string("[::1]:2222").unwrap();
        assert_eq!(server.host, "::1");
        assert_eq!(server.port, 2222);

        let server = K3sServer::from_string("[2001:db8::1]:8080").unwrap();
        assert_eq!(server.host, "2001:db8::1");
        assert_eq!(server.port, 8080);

        let server = K3sServer::from_string("[fe80::1]:3000").unwrap();
        assert_eq!(server.host, "fe80::1");
        assert_eq!(server.port, 3000);

        let server = K3sServer::from_string("[fe80::1%eth0]:3000").unwrap();
        assert_eq!(server.host, "fe80::1%eth0");
        assert_eq!(server.port, 3000);
    }

    #[test]
    fn test_k3s_server_from_string_bare_ipv6() {
        let server = K3sServer::from_string("::1").unwrap();
        assert_eq!(server.host, "::1");
        assert_eq!(server.port, 22);

        let server = K3sServer::from_string("2001:db8::1").unwrap();
        assert_eq!(server.host, "2001:db8::1");
        assert_eq!(server.port, 22);

        let server = K3sServer::from_string("fe80::1").unwrap();
        assert_eq!(server.host, "fe80::1");
        assert_eq!(server.port, 22);

        let server = K3sServer::from_string("2001:0db8:85a3:0000:0000:8a2e:0370:7334").unwrap();
        assert_eq!(server.host, "2001:0db8:85a3:0000:0000:8a2e:0370:7334");
        assert_eq!(server.port, 22);
    }

    #[test]
    fn test_k3s_server_from_string_errors() {
        assert!(K3sServer::from_string("10.101.0.10:invalid").is_err());
        assert!(K3sServer::from_string("[::1]").is_err());
        assert!(K3sServer::from_string("[::1:22").is_err());
        assert!(K3sServer::from_string("").is_err());
        assert!(K3sServer::from_string("example.com:99999").is_err());
        assert!(K3sServer::from_string("[2001:db8::1]:invalid").is_err());
        assert!(K3sServer::from_string("[::]").is_err());
        assert!(K3sServer::from_string("[").is_err());
    }

    #[test]
    fn test_k3s_server_to_string_ipv4() {
        let server = K3sServer::new("10.101.0.10".to_string(), 22);
        assert_eq!(server.to_string(), "10.101.0.10");

        let server = K3sServer::new("10.101.0.10".to_string(), 2222);
        assert_eq!(server.to_string(), "10.101.0.10:2222");
    }

    #[test]
    fn test_k3s_server_to_string_hostname() {
        let server = K3sServer::new("example.com".to_string(), 22);
        assert_eq!(server.to_string(), "example.com");

        let server = K3sServer::new("example.com".to_string(), 8080);
        assert_eq!(server.to_string(), "example.com:8080");
    }

    #[test]
    fn test_k3s_server_to_string_ipv6() {
        let server = K3sServer::new("::1".to_string(), 22);
        assert_eq!(server.to_string(), "::1");

        let server = K3sServer::new("::1".to_string(), 2222);
        assert_eq!(server.to_string(), "[::1]:2222");

        let server = K3sServer::new("2001:db8::1".to_string(), 8080);
        assert_eq!(server.to_string(), "[2001:db8::1]:8080");
    }

    #[test]
    fn test_k3s_server_roundtrip_ipv4() {
        let original = "10.101.0.10:2222";
        let server = K3sServer::from_string(original).unwrap();
        assert_eq!(server.to_string(), original);
    }

    #[test]
    fn test_k3s_server_roundtrip_ipv6() {
        let original = "[::1]:2222";
        let server = K3sServer::from_string(original).unwrap();
        assert_eq!(server.to_string(), original);

        let original = "[2001:db8::1]:8080";
        let server = K3sServer::from_string(original).unwrap();
        assert_eq!(server.to_string(), original);
    }

    #[test]
    fn test_k3s_server_roundtrip_bare_ipv6() {
        let original = "::1";
        let server = K3sServer::from_string(original).unwrap();
        assert_eq!(server.to_string(), original);
    }
}
