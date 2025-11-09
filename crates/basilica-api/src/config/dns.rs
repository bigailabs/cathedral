use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DnsConfig {
    /// Enable DNS management for public deployments
    #[serde(default = "default_enabled")]
    pub enabled: bool,

    /// Cloudflare API token (loaded from environment: CLOUDFLARE_API_TOKEN)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub api_token: Option<String>,

    /// Cloudflare zone ID (loaded from environment: CLOUDFLARE_ZONE_ID)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub zone_id: Option<String>,

    /// Base domain for deployments (e.g., "deployments.basilica.ai")
    #[serde(default = "default_domain")]
    pub domain: String,

    /// Enable Cloudflare proxy mode (DDoS protection + SSL termination)
    #[serde(default = "default_proxy")]
    pub proxy: bool,

    /// ALB DNS name (loaded from environment: ALB_DNS_NAME)
    /// This is the target for all CNAME records
    #[serde(skip_serializing_if = "Option::is_none")]
    pub alb_dns_name: Option<String>,
}

fn default_enabled() -> bool {
    true
}

fn default_domain() -> String {
    "deployments.basilica.ai".to_string()
}

fn default_proxy() -> bool {
    true
}

impl Default for DnsConfig {
    fn default() -> Self {
        Self {
            enabled: default_enabled(),
            api_token: None,
            zone_id: None,
            domain: default_domain(),
            proxy: default_proxy(),
            alb_dns_name: None,
        }
    }
}

impl DnsConfig {
    pub fn from_env(&self) -> Self {
        Self {
            enabled: self.enabled,
            api_token: std::env::var("CLOUDFLARE_API_TOKEN")
                .ok()
                .or(self.api_token.clone()),
            zone_id: std::env::var("CLOUDFLARE_ZONE_ID")
                .ok()
                .or(self.zone_id.clone()),
            domain: std::env::var("CLOUDFLARE_DOMAIN").unwrap_or_else(|_| self.domain.clone()),
            proxy: std::env::var("CLOUDFLARE_PROXY")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(self.proxy),
            alb_dns_name: std::env::var("ALB_DNS_NAME")
                .ok()
                .or(self.alb_dns_name.clone()),
        }
    }

    pub fn validate(&self) -> Result<(), String> {
        if !self.enabled {
            return Ok(());
        }

        if self.api_token.is_none() {
            return Err("DNS enabled but CLOUDFLARE_API_TOKEN not set".to_string());
        }

        if self.zone_id.is_none() {
            return Err("DNS enabled but CLOUDFLARE_ZONE_ID not set".to_string());
        }

        if self.alb_dns_name.is_none() {
            return Err("DNS enabled but ALB_DNS_NAME not set".to_string());
        }

        Ok(())
    }

    pub fn build_public_url(&self, subdomain: &str) -> Option<String> {
        if !self.enabled {
            return None;
        }

        Some(format!("https://{}.{}", subdomain, self.domain))
    }

    pub fn build_fqdn(&self, subdomain: &str) -> Option<String> {
        if !self.enabled {
            return None;
        }

        Some(format!("{}.{}", subdomain, self.domain))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_config() {
        let config = DnsConfig::default();
        assert!(!config.enabled);
        assert_eq!(config.domain, "deployments.basilica.ai");
        assert!(config.proxy);
        assert!(config.api_token.is_none());
        assert!(config.zone_id.is_none());
        assert!(config.alb_dns_name.is_none());
    }

    #[test]
    fn test_build_public_url_disabled() {
        let config = DnsConfig {
            enabled: false,
            ..Default::default()
        };
        assert!(config.build_public_url("abc-123").is_none());
    }

    #[test]
    fn test_build_public_url_enabled() {
        let config = DnsConfig {
            enabled: true,
            domain: "deployments.basilica.ai".to_string(),
            ..Default::default()
        };
        assert_eq!(
            config.build_public_url("abc-123"),
            Some("https://abc-123.deployments.basilica.ai".to_string())
        );
    }

    #[test]
    fn test_validate_disabled() {
        let config = DnsConfig {
            enabled: false,
            ..Default::default()
        };
        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_validate_enabled_missing_api_token() {
        let config = DnsConfig {
            enabled: true,
            api_token: None,
            zone_id: Some("zone-123".to_string()),
            alb_dns_name: Some("alb.example.com".to_string()),
            ..Default::default()
        };
        assert!(config.validate().is_err());
    }

    #[test]
    fn test_validate_enabled_missing_zone_id() {
        let config = DnsConfig {
            enabled: true,
            api_token: Some("token-123".to_string()),
            zone_id: None,
            alb_dns_name: Some("alb.example.com".to_string()),
            ..Default::default()
        };
        assert!(config.validate().is_err());
    }

    #[test]
    fn test_validate_enabled_missing_alb_dns() {
        let config = DnsConfig {
            enabled: true,
            api_token: Some("token-123".to_string()),
            zone_id: Some("zone-123".to_string()),
            alb_dns_name: None,
            ..Default::default()
        };
        assert!(config.validate().is_err());
    }

    #[test]
    fn test_validate_enabled_all_fields() {
        let config = DnsConfig {
            enabled: true,
            api_token: Some("token-123".to_string()),
            zone_id: Some("zone-123".to_string()),
            alb_dns_name: Some("alb.example.com".to_string()),
            ..Default::default()
        };
        assert!(config.validate().is_ok());
    }

    #[test]
    fn test_config_serialization() {
        let config = DnsConfig {
            enabled: true,
            domain: "test.basilica.ai".to_string(),
            proxy: false,
            ..Default::default()
        };
        let serialized = toml::to_string(&config).unwrap();
        let deserialized: DnsConfig = toml::from_str(&serialized).unwrap();

        assert_eq!(config.enabled, deserialized.enabled);
        assert_eq!(config.domain, deserialized.domain);
        assert_eq!(config.proxy, deserialized.proxy);
    }
}
