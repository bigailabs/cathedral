use super::{DnsError, DnsProvider, Result};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use tracing::{debug, info, warn};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CloudflareConfig {
    pub api_token: String,
    pub zone_id: String,
    pub domain: String,
    pub proxy: bool,
}

impl CloudflareConfig {
    pub fn from_env() -> Result<Self> {
        let api_token = std::env::var("CLOUDFLARE_API_TOKEN")
            .map_err(|_| DnsError::ConfigError("CLOUDFLARE_API_TOKEN not set".into()))?;

        let zone_id = std::env::var("CLOUDFLARE_ZONE_ID")
            .map_err(|_| DnsError::ConfigError("CLOUDFLARE_ZONE_ID not set".into()))?;

        let domain = std::env::var("CLOUDFLARE_DOMAIN")
            .unwrap_or_else(|_| "deployments.basilica.ai".to_string());

        let proxy = std::env::var("CLOUDFLARE_PROXY")
            .unwrap_or_else(|_| "true".to_string())
            .parse()
            .unwrap_or(true);

        Ok(Self {
            api_token,
            zone_id,
            domain,
            proxy,
        })
    }
}

#[derive(Debug, Clone)]
pub struct CloudflareDnsManager {
    client: reqwest::Client,
    api_token: String,
    zone_id: String,
    domain: String,
    proxy: bool,
}

#[derive(Debug, Serialize)]
struct CreateDnsRecordRequest {
    #[serde(rename = "type")]
    record_type: String,
    name: String,
    content: String,
    proxied: bool,
    ttl: u32,
}

#[derive(Debug, Deserialize)]
struct DnsRecordResponse {
    #[allow(dead_code)]
    result: Option<DnsRecord>,
    success: bool,
    errors: Vec<CloudflareError>,
}

#[derive(Debug, Deserialize)]
struct ListDnsRecordsResponse {
    result: Vec<DnsRecord>,
    success: bool,
    errors: Vec<CloudflareError>,
}

#[derive(Debug, Deserialize)]
struct DnsRecord {
    id: String,
    #[allow(dead_code)]
    name: String,
}

#[derive(Debug, Deserialize)]
struct CloudflareError {
    code: i32,
    message: String,
}

#[derive(Debug, Deserialize)]
struct DeleteDnsRecordResponse {
    success: bool,
    errors: Vec<CloudflareError>,
}

impl CloudflareDnsManager {
    pub fn new(config: CloudflareConfig) -> Result<Self> {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .build()
            .map_err(|e| DnsError::ConfigError(format!("Failed to create HTTP client: {}", e)))?;

        Ok(Self {
            client,
            api_token: config.api_token,
            zone_id: config.zone_id,
            domain: config.domain,
            proxy: config.proxy,
        })
    }

    fn build_fqdn(&self, subdomain: &str) -> String {
        format!("{}.{}", subdomain, self.domain)
    }

    async fn find_record_id(&self, name: &str) -> Result<Option<String>> {
        let url = format!(
            "https://api.cloudflare.com/client/v4/zones/{}/dns_records?name={}",
            self.zone_id, name
        );

        let response = self
            .client
            .get(&url)
            .header("Authorization", format!("Bearer {}", self.api_token))
            .header("Content-Type", "application/json")
            .send()
            .await
            .map_err(|e| DnsError::ApiError(format!("Failed to list DNS records: {}", e)))?;

        if !response.status().is_success() {
            let status = response.status();
            let text = response.text().await.unwrap_or_default();
            return Err(DnsError::ApiError(format!(
                "API request failed with status {}: {}",
                status, text
            )));
        }

        let records_response: ListDnsRecordsResponse = response
            .json()
            .await
            .map_err(|e| DnsError::ApiError(format!("Failed to parse response: {}", e)))?;

        if !records_response.success {
            let errors = records_response
                .errors
                .iter()
                .map(|e| format!("{}: {}", e.code, e.message))
                .collect::<Vec<_>>()
                .join(", ");
            return Err(DnsError::ApiError(format!(
                "Cloudflare API errors: {}",
                errors
            )));
        }

        if let Some(record) = records_response.result.first() {
            debug!("Found existing DNS record: {} (ID: {})", name, record.id);
            Ok(Some(record.id.clone()))
        } else {
            debug!("No existing DNS record found for: {}", name);
            Ok(None)
        }
    }
}

#[async_trait]
impl DnsProvider for CloudflareDnsManager {
    async fn create_record(&self, subdomain: &str, target: &str) -> Result<String> {
        let fqdn = self.build_fqdn(subdomain);

        if let Some(existing_id) = self.find_record_id(&fqdn).await? {
            warn!(
                "DNS record already exists for {}: {}. Skipping creation.",
                fqdn, existing_id
            );
            return Ok(fqdn);
        }

        info!(
            "Creating DNS CNAME record: {} -> {} (proxy: {})",
            fqdn, target, self.proxy
        );

        let url = format!(
            "https://api.cloudflare.com/client/v4/zones/{}/dns_records",
            self.zone_id
        );

        let request_body = CreateDnsRecordRequest {
            record_type: "CNAME".to_string(),
            name: fqdn.clone(),
            content: target.to_string(),
            proxied: self.proxy,
            ttl: 1,
        };

        let response = self
            .client
            .post(&url)
            .header("Authorization", format!("Bearer {}", self.api_token))
            .header("Content-Type", "application/json")
            .json(&request_body)
            .send()
            .await
            .map_err(|e| DnsError::CreateFailed(format!("API request failed: {}", e)))?;

        if !response.status().is_success() {
            let status = response.status();
            let text = response.text().await.unwrap_or_default();
            return Err(DnsError::CreateFailed(format!(
                "API request failed with status {}: {}",
                status, text
            )));
        }

        let dns_response: DnsRecordResponse = response
            .json()
            .await
            .map_err(|e| DnsError::CreateFailed(format!("Failed to parse response: {}", e)))?;

        if !dns_response.success {
            let errors = dns_response
                .errors
                .iter()
                .map(|e| format!("{}: {}", e.code, e.message))
                .collect::<Vec<_>>()
                .join(", ");
            return Err(DnsError::CreateFailed(format!(
                "Cloudflare API errors: {}",
                errors
            )));
        }

        info!("Successfully created DNS record: {}", fqdn);
        Ok(fqdn)
    }

    async fn delete_record(&self, subdomain: &str) -> Result<()> {
        let fqdn = self.build_fqdn(subdomain);

        let record_id = self
            .find_record_id(&fqdn)
            .await?
            .ok_or_else(|| DnsError::RecordNotFound(fqdn.clone()))?;

        info!("Deleting DNS record: {} (ID: {})", fqdn, record_id);

        let url = format!(
            "https://api.cloudflare.com/client/v4/zones/{}/dns_records/{}",
            self.zone_id, record_id
        );

        let response = self
            .client
            .delete(&url)
            .header("Authorization", format!("Bearer {}", self.api_token))
            .header("Content-Type", "application/json")
            .send()
            .await
            .map_err(|e| DnsError::DeleteFailed(format!("API request failed: {}", e)))?;

        if !response.status().is_success() {
            let status = response.status();
            let text = response.text().await.unwrap_or_default();
            return Err(DnsError::DeleteFailed(format!(
                "API request failed with status {}: {}",
                status, text
            )));
        }

        let delete_response: DeleteDnsRecordResponse = response
            .json()
            .await
            .map_err(|e| DnsError::DeleteFailed(format!("Failed to parse response: {}", e)))?;

        if !delete_response.success {
            let errors = delete_response
                .errors
                .iter()
                .map(|e| format!("{}: {}", e.code, e.message))
                .collect::<Vec<_>>()
                .join(", ");
            return Err(DnsError::DeleteFailed(format!(
                "Cloudflare API errors: {}",
                errors
            )));
        }

        info!("Successfully deleted DNS record: {}", fqdn);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_build_fqdn() {
        let config = CloudflareConfig {
            api_token: "test-token".to_string(),
            zone_id: "test-zone".to_string(),
            domain: "deployments.basilica.ai".to_string(),
            proxy: true,
        };

        let manager = CloudflareDnsManager::new(config).unwrap();
        assert_eq!(
            manager.build_fqdn("abc-123"),
            "abc-123.deployments.basilica.ai"
        );
    }

    #[test]
    fn test_config_from_env_missing_token() {
        std::env::remove_var("CLOUDFLARE_API_TOKEN");
        std::env::remove_var("CLOUDFLARE_ZONE_ID");

        let result = CloudflareConfig::from_env();
        assert!(result.is_err());
        assert!(matches!(result.unwrap_err(), DnsError::ConfigError(_)));
    }
}
