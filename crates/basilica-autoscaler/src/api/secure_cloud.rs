use async_trait::async_trait;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicU32, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::RwLock;
use tracing::{debug, error, info, warn};

use crate::crd::NodeManagedBy;
use crate::error::{AutoscalerError, Result};

/// Circuit breaker state
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CircuitState {
    /// Circuit is closed, requests flow normally
    Closed,
    /// Circuit is open, requests fail fast
    Open,
    /// Circuit is half-open, testing if service recovered
    HalfOpen,
}

/// Circuit breaker for API resilience
#[derive(Debug)]
pub struct CircuitBreaker {
    state: RwLock<CircuitState>,
    failure_count: AtomicU32,
    success_count: AtomicU32,
    last_failure_epoch_ms: AtomicU64,
    config: CircuitBreakerConfig,
}

/// Circuit breaker configuration
#[derive(Clone, Debug)]
pub struct CircuitBreakerConfig {
    /// Number of failures before opening circuit
    pub failure_threshold: u32,
    /// Time to wait before trying again (half-open)
    pub reset_timeout: Duration,
    /// Number of successful calls to close circuit from half-open
    pub success_threshold: u32,
}

impl Default for CircuitBreakerConfig {
    fn default() -> Self {
        Self {
            failure_threshold: 5,
            reset_timeout: Duration::from_secs(30),
            success_threshold: 2,
        }
    }
}

impl CircuitBreaker {
    pub fn new(config: CircuitBreakerConfig) -> Self {
        Self {
            state: RwLock::new(CircuitState::Closed),
            failure_count: AtomicU32::new(0),
            success_count: AtomicU32::new(0),
            last_failure_epoch_ms: AtomicU64::new(0),
            config,
        }
    }

    /// Check if request should proceed
    pub async fn should_allow(&self) -> bool {
        let state = *self.state.read().await;
        match state {
            CircuitState::Closed => true,
            CircuitState::Open => {
                let last_failure = self.last_failure_epoch_ms.load(Ordering::SeqCst);
                let now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_millis() as u64)
                    .unwrap_or(0);
                let elapsed = Duration::from_millis(now.saturating_sub(last_failure));

                if elapsed >= self.config.reset_timeout {
                    let mut state = self.state.write().await;
                    if *state == CircuitState::Open {
                        *state = CircuitState::HalfOpen;
                        info!("Circuit breaker transitioning to half-open");
                    }
                    true
                } else {
                    false
                }
            }
            CircuitState::HalfOpen => true,
        }
    }

    /// Record a successful call
    pub async fn record_success(&self) {
        let mut state = self.state.write().await;
        match *state {
            CircuitState::HalfOpen => {
                let count = self.success_count.fetch_add(1, Ordering::SeqCst) + 1;
                if count >= self.config.success_threshold {
                    *state = CircuitState::Closed;
                    self.failure_count.store(0, Ordering::SeqCst);
                    self.success_count.store(0, Ordering::SeqCst);
                    info!("Circuit breaker closed after {} successful calls", count);
                } else {
                    debug!(
                        "Circuit breaker half-open: {}/{} successful calls",
                        count, self.config.success_threshold
                    );
                }
            }
            CircuitState::Closed => {
                self.failure_count.store(0, Ordering::SeqCst);
            }
            CircuitState::Open => {}
        }
    }

    /// Record a failed call
    pub async fn record_failure(&self) {
        let count = self.failure_count.fetch_add(1, Ordering::SeqCst) + 1;
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0);
        self.last_failure_epoch_ms.store(now, Ordering::SeqCst);

        let mut state = self.state.write().await;
        match *state {
            CircuitState::Closed if count >= self.config.failure_threshold => {
                *state = CircuitState::Open;
                error!(
                    failures = count,
                    threshold = self.config.failure_threshold,
                    "Circuit breaker opened"
                );
            }
            CircuitState::HalfOpen => {
                *state = CircuitState::Open;
                self.success_count.store(0, Ordering::SeqCst);
                warn!("Circuit breaker reopened after failure in half-open state");
            }
            _ => {}
        }
    }

    /// Get current state (useful for diagnostics)
    #[allow(dead_code)]
    pub async fn get_state(&self) -> CircuitState {
        *self.state.read().await
    }
}

/// Deserialize a value that can be either a string or a number into f64
fn deserialize_string_or_f64<'de, D>(deserializer: D) -> std::result::Result<f64, D::Error>
where
    D: serde::Deserializer<'de>,
{
    use serde::de::{self, Visitor};

    struct StringOrF64Visitor;

    impl<'de> Visitor<'de> for StringOrF64Visitor {
        type Value = f64;

        fn expecting(&self, formatter: &mut std::fmt::Formatter) -> std::fmt::Result {
            formatter.write_str("a string or number representing a float")
        }

        fn visit_f64<E: de::Error>(self, v: f64) -> std::result::Result<f64, E> {
            Ok(v)
        }

        fn visit_i64<E: de::Error>(self, v: i64) -> std::result::Result<f64, E> {
            Ok(v as f64)
        }

        fn visit_u64<E: de::Error>(self, v: u64) -> std::result::Result<f64, E> {
            Ok(v as f64)
        }

        fn visit_str<E: de::Error>(self, v: &str) -> std::result::Result<f64, E> {
            v.parse::<f64>().map_err(de::Error::custom)
        }
    }

    deserializer.deserialize_any(StringOrF64Visitor)
}

/// Response wrapper for GPU prices endpoint
#[derive(Clone, Debug, Deserialize)]
pub struct GpuPricesResponse {
    pub nodes: Vec<GpuOffering>,
    pub count: usize,
}

/// GPU offering from Secure Cloud API
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct GpuOffering {
    pub id: String,
    pub gpu_type: String,
    pub gpu_count: u32,
    #[serde(default)]
    pub gpu_memory_gb_per_gpu: Option<u32>,
    #[serde(alias = "hourly_rate", deserialize_with = "deserialize_string_or_f64")]
    pub hourly_rate_per_gpu: f64,
    pub provider: String,
    pub region: String,
    #[serde(alias = "available")]
    pub availability: bool,
}

impl GpuOffering {
    /// Get GPU memory in GB (convenience accessor)
    pub fn gpu_memory_gb(&self) -> u32 {
        self.gpu_memory_gb_per_gpu.unwrap_or(0)
    }

    /// Get hourly rate (convenience accessor for backwards compatibility)
    pub fn hourly_rate(&self) -> f64 {
        self.hourly_rate_per_gpu
    }

    /// Check if available (convenience accessor for backwards compatibility)
    pub fn available(&self) -> bool {
        self.availability
    }
}

/// Rental information from Secure Cloud API
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct RentalInfo {
    pub rental_id: String,
    pub deployment_id: String,
    pub provider: String,
    pub status: String,
    #[serde(default)]
    pub ip_address: Option<String>,
    #[serde(default)]
    pub ssh_command: Option<String>,
    pub hourly_cost: f64,
}

/// Response from listing secure cloud rentals
#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
struct ListRentalsResponse {
    rentals: Vec<RentalListItem>,
    #[serde(default)]
    #[allow(dead_code)]
    total_count: usize,
}

/// Rental list item from API (subset of fields we need)
#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
struct RentalListItem {
    rental_id: String,
    provider: String,
    #[serde(default)]
    provider_instance_id: Option<String>,
    status: String,
    #[serde(default)]
    ip_address: Option<String>,
    #[serde(default)]
    hourly_cost: f64,
}

/// Node registration request
#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub struct NodeRegistrationRequest {
    pub node_id: String,
    pub datacenter_id: String,
    pub gpu_specs: GpuSpecs,
}

/// GPU specifications for node registration
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct GpuSpecs {
    pub count: u32,
    pub model: String,
    pub memory_gb: u32,
    pub driver_version: String,
    pub cuda_version: String,
}

/// Node registration response from API
#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct NodeRegistrationResponse {
    pub node_id: String,
    pub k3s_url: String,
    pub k3s_token: String,
    #[serde(default)]
    pub node_password: Option<String>,
    pub node_labels: std::collections::HashMap<String, String>,
    pub status: String,
    #[serde(default)]
    pub wireguard: Option<WireGuardConfigResponse>,
}

/// WireGuard configuration from API
#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct WireGuardConfigResponse {
    pub enabled: bool,
    pub node_ip: String,
    pub peers: Vec<WireGuardPeer>,
    #[serde(default)]
    pub persistent_keepalive: u32,
}

/// WireGuard peer configuration
#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub struct WireGuardPeer {
    pub endpoint: String,
    pub public_key: String,
    pub wireguard_ip: String,
    pub vpc_subnet: String,
    #[serde(default)]
    pub route_pod_network: bool,
}

impl WireGuardPeer {
    /// Compute allowed IPs for WireGuard config from peer fields.
    /// Pod network (10.42.0.0/16) is ALWAYS added to all peers for HA.
    /// WireGuard uses longest-prefix matching, so traffic routes to
    /// whichever peer has an active handshake (prevents single point of failure).
    pub fn allowed_ips(&self) -> String {
        let ips = [
            format!("{}/32", self.wireguard_ip),
            self.vpc_subnet.clone(),
            "10.42.0.0/16".to_string(),
        ];
        ips.join(", ")
    }
}

/// WireGuard key registration request
#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub struct WireGuardKeyRequest {
    pub public_key: String,
}

/// WireGuard key registration response
#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct WireGuardRegistrationResponse {
    pub status: String,
}

/// Rental start request
#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub struct StartRentalRequest {
    pub offering_id: String,
    pub ssh_public_key_id: String,
}

/// Trait for Secure Cloud API operations
#[async_trait]
pub trait SecureCloudApi: Send + Sync {
    /// List available GPU offerings
    async fn list_offerings(&self) -> Result<Vec<GpuOffering>>;

    /// Get a specific offering by ID
    async fn get_offering(&self, offering_id: &str) -> Result<Option<GpuOffering>>;

    /// Start a rental
    async fn start_rental(&self, offering_id: &str, ssh_key_id: &str) -> Result<RentalInfo>;

    /// Get rental status (used to poll for IP address)
    async fn get_rental(&self, rental_id: &str) -> Result<Option<RentalInfo>>;

    /// Stop a rental
    async fn stop_rental(&self, rental_id: &str) -> Result<()>;

    /// Register a node (idempotent)
    async fn register_node(
        &self,
        request: NodeRegistrationRequest,
    ) -> Result<NodeRegistrationResponse>;

    /// Register WireGuard public key
    async fn register_wireguard_key(
        &self,
        node_id: &str,
        public_key: &str,
    ) -> Result<WireGuardRegistrationResponse>;

    /// Deregister a node
    async fn deregister_node(&self, node_id: &str) -> Result<()>;

    /// Get updated peer list
    async fn get_peers(&self, node_id: &str) -> Result<Vec<WireGuardPeer>>;
}

/// HTTP client implementation for Secure Cloud API with circuit breaker
pub struct SecureCloudClient {
    client: reqwest::Client,
    base_url: String,
    api_key: String,
    circuit_breaker: Arc<CircuitBreaker>,
}

impl Clone for SecureCloudClient {
    fn clone(&self) -> Self {
        Self {
            client: self.client.clone(),
            base_url: self.base_url.clone(),
            api_key: self.api_key.clone(),
            circuit_breaker: Arc::clone(&self.circuit_breaker),
        }
    }
}

impl SecureCloudClient {
    /// Create a new Secure Cloud API client
    pub fn new(base_url: String, api_key: String, timeout: Duration) -> Self {
        let client = reqwest::Client::builder()
            .timeout(timeout)
            .build()
            .expect("Failed to create HTTP client");

        Self {
            client,
            base_url,
            api_key,
            circuit_breaker: Arc::new(CircuitBreaker::new(CircuitBreakerConfig::default())),
        }
    }

    /// Create with custom circuit breaker config
    pub fn with_circuit_breaker(
        base_url: String,
        api_key: String,
        timeout: Duration,
        cb_config: CircuitBreakerConfig,
    ) -> Self {
        let client = reqwest::Client::builder()
            .timeout(timeout)
            .build()
            .expect("Failed to create HTTP client");

        Self {
            client,
            base_url,
            api_key,
            circuit_breaker: Arc::new(CircuitBreaker::new(cb_config)),
        }
    }

    /// Create from environment variables
    pub fn from_env() -> Result<Self> {
        let base_url = std::env::var("BASILICA_API_URL")
            .unwrap_or_else(|_| "https://api.basilica.ai".to_string());
        let api_key = std::env::var("BASILICA_API_KEY").map_err(|_| {
            AutoscalerError::InvalidConfiguration("BASILICA_API_KEY not set".to_string())
        })?;
        let timeout = std::env::var("BASILICA_API_TIMEOUT_SECS")
            .ok()
            .and_then(|s| s.parse::<u64>().ok())
            .map(Duration::from_secs)
            .unwrap_or(Duration::from_secs(30));

        let cb_config = CircuitBreakerConfig {
            failure_threshold: std::env::var("BASILICA_API_CB_FAILURE_THRESHOLD")
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(5),
            reset_timeout: std::env::var("BASILICA_API_CB_RESET_TIMEOUT_SECS")
                .ok()
                .and_then(|s| s.parse::<u64>().ok())
                .map(Duration::from_secs)
                .unwrap_or(Duration::from_secs(30)),
            success_threshold: 2,
        };

        Ok(Self::with_circuit_breaker(
            base_url, api_key, timeout, cb_config,
        ))
    }

    fn auth_header(&self) -> String {
        format!("Bearer {}", self.api_key)
    }

    /// Check circuit breaker before making request
    async fn check_circuit(&self) -> Result<()> {
        if !self.circuit_breaker.should_allow().await {
            Err(AutoscalerError::CircuitBreakerOpen(
                "API circuit breaker is open, failing fast".to_string(),
            ))
        } else {
            Ok(())
        }
    }
}

#[async_trait]
impl SecureCloudApi for SecureCloudClient {
    async fn list_offerings(&self) -> Result<Vec<GpuOffering>> {
        self.check_circuit().await?;
        let url = format!("{}/secure-cloud/gpu-prices", self.base_url);
        debug!("Listing GPU offerings from {}", url);

        let result = async {
            let response = self
                .client
                .get(&url)
                .header("Authorization", self.auth_header())
                .send()
                .await?;

            if !response.status().is_success() {
                let status = response.status();
                let body = response.text().await.unwrap_or_default();
                return Err(AutoscalerError::SecureCloudApi(format!(
                    "Failed to list offerings: {} - {}",
                    status, body
                )));
            }

            let response_body: GpuPricesResponse = response.json().await?;
            info!("Retrieved {} GPU offerings", response_body.count);
            Ok(response_body.nodes)
        }
        .await;

        match &result {
            Ok(_) => self.circuit_breaker.record_success().await,
            Err(_) => self.circuit_breaker.record_failure().await,
        }
        result
    }

    async fn get_offering(&self, offering_id: &str) -> Result<Option<GpuOffering>> {
        let offerings = self.list_offerings().await?;
        Ok(offerings.into_iter().find(|o| o.id == offering_id))
    }

    async fn start_rental(&self, offering_id: &str, ssh_key_id: &str) -> Result<RentalInfo> {
        self.check_circuit().await?;
        let url = format!("{}/secure-cloud/rentals/start", self.base_url);
        info!("Starting rental for offering {}", offering_id);

        let request = StartRentalRequest {
            offering_id: offering_id.to_string(),
            ssh_public_key_id: ssh_key_id.to_string(),
        };

        let result = async {
            let response = self
                .client
                .post(&url)
                .header("Authorization", self.auth_header())
                .json(&request)
                .send()
                .await?;

            if !response.status().is_success() {
                let status = response.status();
                let body = response.text().await.unwrap_or_default();
                return Err(AutoscalerError::RentalStart(format!(
                    "Failed to start rental: {} - {}",
                    status, body
                )));
            }

            let rental: RentalInfo = response.json().await?;
            info!("Rental started: {}", rental.rental_id);
            Ok(rental)
        }
        .await;

        match &result {
            Ok(_) => self.circuit_breaker.record_success().await,
            Err(_) => self.circuit_breaker.record_failure().await,
        }
        result
    }

    async fn get_rental(&self, rental_id: &str) -> Result<Option<RentalInfo>> {
        self.check_circuit().await?;
        let url = format!("{}/secure-cloud/rentals", self.base_url);
        debug!("Fetching rental {} from list", rental_id);

        let result = async {
            let response = self
                .client
                .get(&url)
                .header("Authorization", self.auth_header())
                .send()
                .await?;

            if !response.status().is_success() {
                let status = response.status();
                let body = response.text().await.unwrap_or_default();
                return Err(AutoscalerError::SecureCloudApi(format!(
                    "Failed to list rentals: {} - {}",
                    status, body
                )));
            }

            let list_response: ListRentalsResponse = response.json().await?;
            let rental = list_response
                .rentals
                .into_iter()
                .find(|r| r.rental_id == rental_id);

            Ok(rental.map(|r| RentalInfo {
                rental_id: r.rental_id,
                deployment_id: r.provider_instance_id.unwrap_or_default(),
                provider: r.provider,
                status: r.status,
                ip_address: r.ip_address,
                ssh_command: None,
                hourly_cost: r.hourly_cost,
            }))
        }
        .await;

        match &result {
            Ok(_) => self.circuit_breaker.record_success().await,
            Err(_) => self.circuit_breaker.record_failure().await,
        }
        result
    }

    async fn stop_rental(&self, rental_id: &str) -> Result<()> {
        self.check_circuit().await?;
        let url = format!("{}/secure-cloud/rentals/{}/stop", self.base_url, rental_id);
        info!("Stopping rental {}", rental_id);

        let result = async {
            let response = self
                .client
                .post(&url)
                .header("Authorization", self.auth_header())
                .send()
                .await?;

            let status = response.status();

            // Treat 404/410 as success - rental is already deleted (idempotent cleanup)
            if status == reqwest::StatusCode::NOT_FOUND || status == reqwest::StatusCode::GONE {
                info!(
                    "Rental {} already deleted ({}), treating as success",
                    rental_id, status
                );
                return Ok(());
            }

            if !status.is_success() {
                let body = response.text().await.unwrap_or_default();
                return Err(AutoscalerError::RentalStop(format!(
                    "Failed to stop rental: {} - {}",
                    status, body
                )));
            }

            info!("Rental {} stopped", rental_id);
            Ok(())
        }
        .await;

        match &result {
            Ok(_) => self.circuit_breaker.record_success().await,
            Err(_) => self.circuit_breaker.record_failure().await,
        }
        result
    }

    async fn register_node(
        &self,
        request: NodeRegistrationRequest,
    ) -> Result<NodeRegistrationResponse> {
        self.check_circuit().await?;
        let url = format!("{}/v1/gpu-nodes/register", self.base_url);
        let node_id = request.node_id.clone();
        info!("Registering node {} with API", node_id);

        let result = async {
            let response = self
                .client
                .post(&url)
                .header("Authorization", self.auth_header())
                .json(&request)
                .send()
                .await?;

            if !response.status().is_success() {
                let status = response.status();
                let body = response.text().await.unwrap_or_default();
                return Err(AutoscalerError::ApiRegistration(format!(
                    "Failed to register node: {} - {}",
                    status, body
                )));
            }

            let reg_response: NodeRegistrationResponse = response.json().await?;
            let wg_ip = reg_response
                .wireguard
                .as_ref()
                .map(|w| w.node_ip.as_str())
                .unwrap_or("none");
            info!(
                "Node {} registered, status: {}, WG IP: {}",
                node_id, reg_response.status, wg_ip
            );
            Ok(reg_response)
        }
        .await;

        match &result {
            Ok(_) => self.circuit_breaker.record_success().await,
            Err(_) => self.circuit_breaker.record_failure().await,
        }
        result
    }

    async fn register_wireguard_key(
        &self,
        node_id: &str,
        public_key: &str,
    ) -> Result<WireGuardRegistrationResponse> {
        self.check_circuit().await?;
        let url = format!("{}/v1/gpu-nodes/{}/wireguard-key", self.base_url, node_id);
        info!("Registering WireGuard public key for node {}", node_id);

        let request = WireGuardKeyRequest {
            public_key: public_key.to_string(),
        };

        let result = async {
            let response = self
                .client
                .post(&url)
                .header("Authorization", self.auth_header())
                .json(&request)
                .send()
                .await?;

            if !response.status().is_success() {
                let status = response.status();
                let body = response.text().await.unwrap_or_default();
                return Err(AutoscalerError::ApiRegistration(format!(
                    "Failed to register WireGuard key: {} - {}",
                    status, body
                )));
            }

            let wg_response: WireGuardRegistrationResponse = response.json().await?;
            if wg_response.status != "peer_added" {
                warn!(
                    "WireGuard key registration returned unexpected status: {}",
                    wg_response.status
                );
            }
            Ok(wg_response)
        }
        .await;

        match &result {
            Ok(_) => self.circuit_breaker.record_success().await,
            Err(_) => self.circuit_breaker.record_failure().await,
        }
        result
    }

    async fn deregister_node(&self, node_id: &str) -> Result<()> {
        self.check_circuit().await?;
        let url = format!("{}/v1/gpu-nodes/{}", self.base_url, node_id);
        info!("Deregistering node {}", node_id);

        let result = async {
            let response = self
                .client
                .delete(&url)
                .header("Authorization", self.auth_header())
                .send()
                .await?;

            if !response.status().is_success() {
                let status = response.status();
                let body = response.text().await.unwrap_or_default();
                // Return error so circuit breaker correctly records failure
                return Err(AutoscalerError::SecureCloudApi(format!(
                    "Failed to deregister node {}: {} - {}",
                    node_id, status, body
                )));
            }

            info!("Node {} deregistered successfully", node_id);
            Ok(())
        }
        .await;

        match &result {
            Ok(_) => self.circuit_breaker.record_success().await,
            Err(_) => self.circuit_breaker.record_failure().await,
        }
        result
    }

    async fn get_peers(&self, node_id: &str) -> Result<Vec<WireGuardPeer>> {
        self.check_circuit().await?;
        let url = format!("{}/v1/gpu-nodes/{}/peers", self.base_url, node_id);
        debug!("Getting peers for node {}", node_id);

        let result = async {
            let response = self
                .client
                .get(&url)
                .header("Authorization", self.auth_header())
                .send()
                .await?;

            if !response.status().is_success() {
                let status = response.status();
                let body = response.text().await.unwrap_or_default();
                return Err(AutoscalerError::SecureCloudApi(format!(
                    "Failed to get peers: {} - {}",
                    status, body
                )));
            }

            let peers: Vec<WireGuardPeer> = response.json().await?;
            debug!("Retrieved {} peers for node {}", peers.len(), node_id);
            Ok(peers)
        }
        .await;

        match &result {
            Ok(_) => self.circuit_breaker.record_success().await,
            Err(_) => self.circuit_breaker.record_failure().await,
        }
        result
    }
}

impl NodeManagedBy {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Autoscaler => "autoscaler",
            Self::OnboardScript => "onboard-script",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn node_managed_by_serializes_correctly() {
        assert_eq!(NodeManagedBy::Autoscaler.as_str(), "autoscaler");
        assert_eq!(NodeManagedBy::OnboardScript.as_str(), "onboard-script");
    }

    #[test]
    fn gpu_offering_deserializes() {
        // Test with API response format (snake_case fields from aggregator)
        let json = r#"{
            "id": "rtx4090-2gpu",
            "gpu_type": "RTX_4090",
            "gpu_count": 2,
            "gpu_memory_gb_per_gpu": 24,
            "hourly_rate_per_gpu": 1.5,
            "provider": "hyperstack",
            "region": "us-east-1",
            "availability": true
        }"#;

        let offering: GpuOffering = serde_json::from_str(json).unwrap();
        assert_eq!(offering.id, "rtx4090-2gpu");
        assert_eq!(offering.gpu_count, 2);
        assert_eq!(offering.gpu_memory_gb(), 24);
        assert_eq!(offering.hourly_rate(), 1.5);
        assert!(offering.available());
    }

    #[test]
    fn gpu_offering_deserializes_with_aliases() {
        // Test with legacy field names (aliases should work)
        let json = r#"{
            "id": "a100-1gpu",
            "gpu_type": "A100",
            "gpu_count": 1,
            "hourly_rate": 2.5,
            "provider": "datacrunch",
            "region": "eu-west-1",
            "available": true
        }"#;

        let offering: GpuOffering = serde_json::from_str(json).unwrap();
        assert_eq!(offering.id, "a100-1gpu");
        assert_eq!(offering.gpu_count, 1);
        assert_eq!(offering.gpu_memory_gb(), 0); // None defaults to 0
        assert_eq!(offering.hourly_rate(), 2.5);
        assert!(offering.available());
    }

    #[test]
    fn gpu_offering_deserializes_string_hourly_rate() {
        // Test with string-formatted hourly_rate (as returned by API's Decimal type)
        let json = r#"{
            "id": "h100-8gpu",
            "gpu_type": "H100",
            "gpu_count": 8,
            "gpu_memory_gb_per_gpu": 80,
            "hourly_rate_per_gpu": "1.76000",
            "provider": "hyperstack",
            "region": "us-west-2",
            "availability": true
        }"#;

        let offering: GpuOffering = serde_json::from_str(json).unwrap();
        assert_eq!(offering.id, "h100-8gpu");
        assert_eq!(offering.gpu_count, 8);
        assert_eq!(offering.gpu_memory_gb(), 80);
        assert!((offering.hourly_rate() - 1.76).abs() < 0.0001);
        assert!(offering.available());
    }

    #[test]
    fn node_registration_request_serializes() {
        let request = NodeRegistrationRequest {
            node_id: "test-node".to_string(),
            datacenter_id: "dc-1".to_string(),
            gpu_specs: GpuSpecs {
                count: 2,
                model: "A100".to_string(),
                memory_gb: 80,
                driver_version: "535.104.05".to_string(),
                cuda_version: "12.2".to_string(),
            },
        };

        let json = serde_json::to_string(&request).unwrap();
        assert!(json.contains("test-node"));
        assert!(json.contains("gpu_specs"));
        assert!(json.contains("A100"));
    }
}
