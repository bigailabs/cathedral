use anyhow::{Context, Result};
use basilica_protocol::billing::{
    billing_service_client::BillingServiceClient, get_active_rentals_request,
    GetActiveRentalsRequest, GetActiveRentalsResponse, GetBalanceRequest, GetBalanceResponse,
    GetBillingPackagesRequest, GetBillingPackagesResponse, UsageAggregation, UsageReportRequest,
    UsageReportResponse,
};
use std::time::Duration;
use tonic::transport::{Channel, ClientTlsConfig, Endpoint};
use tracing::{debug, info, warn};

const DEFAULT_TIMEOUT_SECS: u64 = 30;
const MAX_RETRIES: u32 = 3;
const INITIAL_BACKOFF_MS: u64 = 500;
const MAX_BACKOFF_SECS: u64 = 10;

#[derive(Clone)]
pub struct BillingClient {
    client: BillingServiceClient<Channel>,
}

impl BillingClient {
    pub async fn new(endpoint_url: impl Into<String>) -> Result<Self> {
        let endpoint_str = endpoint_url.into();
        info!("Initializing billing client for endpoint: {}", endpoint_str);

        let channel = Self::connect_with_retry(&endpoint_str).await?;

        let client = BillingServiceClient::new(channel);

        info!("Successfully connected to billing service");

        Ok(Self { client })
    }

    pub async fn new_with_tls(endpoint_url: impl Into<String>) -> Result<Self> {
        let endpoint_str = endpoint_url.into();
        info!(
            "Initializing billing client with TLS for endpoint: {}",
            endpoint_str
        );

        let channel = Self::connect_with_tls_and_retry(&endpoint_str).await?;

        let client = BillingServiceClient::new(channel);

        info!("Successfully connected to billing service with TLS");

        Ok(Self { client })
    }

    async fn connect_with_retry(endpoint_url: &str) -> Result<Channel> {
        let endpoint = Endpoint::from_shared(endpoint_url.to_string())
            .with_context(|| format!("Invalid billing endpoint: {}", endpoint_url))?
            .connect_timeout(Duration::from_secs(DEFAULT_TIMEOUT_SECS))
            .timeout(Duration::from_secs(DEFAULT_TIMEOUT_SECS));

        Self::connect_endpoint_with_retry(endpoint).await
    }

    async fn connect_with_tls_and_retry(endpoint_url: &str) -> Result<Channel> {
        let host = endpoint_url
            .trim_start_matches("https://")
            .trim_start_matches("http://")
            .split([':', '/'].as_ref())
            .next()
            .ok_or_else(|| anyhow::anyhow!("Invalid TLS endpoint: {}", endpoint_url))?;

        let endpoint = Endpoint::from_shared(endpoint_url.to_string())
            .with_context(|| format!("Invalid billing endpoint: {}", endpoint_url))?
            .connect_timeout(Duration::from_secs(DEFAULT_TIMEOUT_SECS))
            .timeout(Duration::from_secs(DEFAULT_TIMEOUT_SECS))
            .tls_config(ClientTlsConfig::new().domain_name(host))
            .with_context(|| "Failed to configure TLS for billing endpoint")?;

        Self::connect_endpoint_with_retry(endpoint).await
    }

    async fn connect_endpoint_with_retry(endpoint: Endpoint) -> Result<Channel> {
        let mut attempt = 0;
        let mut backoff = Duration::from_millis(INITIAL_BACKOFF_MS);

        loop {
            match endpoint.connect().await {
                Ok(channel) => {
                    debug!("Connected to billing service on attempt {}", attempt + 1);
                    return Ok(channel);
                }
                Err(e) => {
                    attempt += 1;
                    if attempt >= MAX_RETRIES {
                        return Err(e).context(format!(
                            "Failed to connect to billing service after {} attempts",
                            MAX_RETRIES
                        ));
                    }

                    warn!(
                        "Failed to connect to billing service (attempt {}/{}): {}. Retrying in {:?}",
                        attempt, MAX_RETRIES, e, backoff
                    );

                    tokio::time::sleep(backoff).await;
                    backoff = (backoff * 2).min(Duration::from_secs(MAX_BACKOFF_SECS));
                }
            }
        }
    }

    pub async fn get_balance(&self, user_id: impl Into<String>) -> Result<GetBalanceResponse> {
        let user_id = user_id.into();
        let request = GetBalanceRequest {
            user_id: user_id.clone(),
        };

        debug!("Fetching balance for user: {}", user_id);

        let mut client = self.client.clone();
        let response = client
            .get_balance(request)
            .await
            .with_context(|| format!("Failed to get balance for user: {}", user_id))?;

        Ok(response.into_inner())
    }

    pub async fn get_billing_packages(&self, user_id: impl Into<String>) -> Result<GetBillingPackagesResponse> {
        let user_id = user_id.into();
        let request = GetBillingPackagesRequest {
            user_id: user_id.clone(),
        };

        debug!("Fetching billing packages for user: {}", user_id);

        let mut client = self.client.clone();
        let response = client
            .get_billing_packages(request)
            .await
            .with_context(|| format!("Failed to get billing packages for user: {}", user_id))?;

        Ok(response.into_inner())
    }

    pub async fn get_usage_report(
        &self,
        rental_id: String,
        start_time: Option<prost_types::Timestamp>,
        end_time: Option<prost_types::Timestamp>,
        aggregation: UsageAggregation,
    ) -> Result<UsageReportResponse> {
        let request = UsageReportRequest {
            rental_id: rental_id.clone(),
            start_time,
            end_time,
            aggregation: aggregation as i32,
        };

        debug!("Fetching usage report for rental: {}", rental_id);

        let mut client = self.client.clone();
        let response = client
            .get_usage_report(request)
            .await
            .with_context(|| format!("Failed to get usage report for rental: {}", rental_id))?;

        Ok(response.into_inner())
    }

    pub async fn get_active_rentals_for_user(
        &self,
        user_id: impl Into<String>,
        limit: Option<u32>,
        offset: Option<u32>,
    ) -> Result<GetActiveRentalsResponse> {
        let user_id = user_id.into();
        let request = GetActiveRentalsRequest {
            filter: Some(get_active_rentals_request::Filter::UserId(
                user_id.clone(),
            )),
            limit: limit.unwrap_or(50),
            offset: offset.unwrap_or(0),
        };

        debug!("Fetching active rentals for user: {}", user_id);

        let mut client = self.client.clone();
        let response = client
            .get_active_rentals(request)
            .await
            .with_context(|| format!("Failed to get active rentals for user: {}", user_id))?;

        Ok(response.into_inner())
    }

    pub async fn get_active_rentals_for_validator(
        &self,
        validator_id: String,
        limit: Option<u32>,
        offset: Option<u32>,
    ) -> Result<GetActiveRentalsResponse> {
        let request = GetActiveRentalsRequest {
            filter: Some(get_active_rentals_request::Filter::ValidatorId(
                validator_id.clone(),
            )),
            limit: limit.unwrap_or(50),
            offset: offset.unwrap_or(0),
        };

        debug!("Fetching active rentals for validator: {}", validator_id);

        let mut client = self.client.clone();
        let response = client.get_active_rentals(request).await.with_context(|| {
            format!(
                "Failed to get active rentals for validator: {}",
                validator_id
            )
        })?;

        Ok(response.into_inner())
    }

    pub async fn get_active_rentals_for_node(
        &self,
        node_id: String,
        limit: Option<u32>,
        offset: Option<u32>,
    ) -> Result<GetActiveRentalsResponse> {
        let request = GetActiveRentalsRequest {
            filter: Some(get_active_rentals_request::Filter::NodeId(node_id.clone())),
            limit: limit.unwrap_or(50),
            offset: offset.unwrap_or(0),
        };

        debug!("Fetching active rentals for node: {}", node_id);

        let mut client = self.client.clone();
        let response = client
            .get_active_rentals(request)
            .await
            .with_context(|| format!("Failed to get active rentals for node: {}", node_id))?;

        Ok(response.into_inner())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_client_construction() {
        assert_eq!(DEFAULT_TIMEOUT_SECS, 30);
        assert_eq!(MAX_RETRIES, 3);
    }
}
