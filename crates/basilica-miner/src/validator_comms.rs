//! # Validator Communications
//!
//! gRPC server for handling validator requests with direct node access.
//! Primary responsibilities:
//! - Authenticate validators using Bittensor signatures
//! - Provide node connection details to authorized validators

use anyhow::{Context, Result};
use rand::Rng;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::sync::RwLock;

use tonic::{transport::Server, Request, Response, Status};
use tonic_health::server::health_reporter;
use tracing::{debug, error, info, warn};

use basilica_common::crypto::verify_signature_bittensor;
use basilica_common::identity::Hotkey;
use basilica_protocol::miner_discovery::{
    miner_discovery_server::{MinerDiscovery, MinerDiscoveryServer},
    DiscoverNodesRequest, ListNodeConnectionDetailsResponse, MinerAuthResponse, MinerBid,
    SubmitBidRequest, SubmitBidResponse, ValidatorAuthRequest,
};

use crate::config::{SecurityConfig, ValidatorCommsConfig};
use crate::node_manager::NodeManager;
use crate::validator_discovery::{ValidatorDiscovery, ValidatorInfo};

#[async_trait::async_trait]
pub trait ValidatorDiscoveryApi: Send + Sync {
    async fn get_active_validators(&self) -> Result<Vec<ValidatorInfo>>;
}

#[async_trait::async_trait]
impl ValidatorDiscoveryApi for ValidatorDiscovery {
    async fn get_active_validators(&self) -> Result<Vec<ValidatorInfo>> {
        self.get_active_validators().await
    }
}

pub trait BittensorServiceApi: Send + Sync {
    fn get_account_id(&self) -> String;
    fn sign_data(&self, data: &[u8]) -> Result<String>;
}

impl BittensorServiceApi for bittensor::Service {
    fn get_account_id(&self) -> String {
        self.get_account_id().to_string()
    }

    fn sign_data(&self, data: &[u8]) -> Result<String> {
        self.sign_data(data).map_err(|e| anyhow::anyhow!(e))
    }
}

/// Validator communications server
#[derive(Clone)]
pub struct ValidatorCommsServer {
    config: ValidatorCommsConfig,
    security_config: SecurityConfig,
    node_manager: Arc<NodeManager>,
    validator_discovery: Arc<dyn ValidatorDiscoveryApi>,
    authenticated_validators: Arc<RwLock<HashMap<String, String>>>,
    bittensor_service: Arc<dyn BittensorServiceApi>,
    miner_hotkey: String,
}

impl ValidatorCommsServer {
    /// Create new validator communications server
    #[allow(clippy::too_many_arguments)]
    pub async fn new(
        config: ValidatorCommsConfig,
        security_config: SecurityConfig,
        node_manager: Arc<NodeManager>,
        validator_discovery: Arc<dyn ValidatorDiscoveryApi>,
        bittensor_service: Arc<dyn BittensorServiceApi>,
    ) -> Result<Self> {
        let miner_hotkey = bittensor_service.get_account_id().to_string();
        Ok(Self {
            config,
            security_config,
            node_manager,
            validator_discovery,
            authenticated_validators: Arc::new(RwLock::new(HashMap::new())),
            bittensor_service,
            miner_hotkey,
        })
    }

    /// Start the gRPC server
    pub async fn start(&self) -> Result<()> {
        let addr: SocketAddr = format!("{}:{}", self.config.host, self.config.port)
            .parse()
            .context("Failed to parse server address")?;

        let (mut health_reporter, health_service) = health_reporter();
        health_reporter
            .set_serving::<MinerDiscoveryServer<MinerDiscoveryService>>()
            .await;

        // Create the discovery service
        let discovery_service = MinerDiscoveryService {
            server: self.clone(),
            bittensor_service: self.bittensor_service.clone(),
        };

        info!("Starting validator communications server on {}", addr);

        Server::builder()
            .add_service(health_service)
            .add_service(MinerDiscoveryServer::new(discovery_service))
            .serve(addr)
            .await
            .context("Failed to start gRPC server")?;

        Ok(())
    }

    /// Get the gRPC server address
    pub fn address(&self) -> String {
        format!("{}:{}", self.config.host, self.config.port)
    }

    /// Check if a validator is authorized
    async fn is_validator_authorized(&self, validator_hotkey: &str) -> bool {
        match self.validator_discovery.get_active_validators().await {
            Ok(validators) => validators.iter().any(|v| v.hotkey == validator_hotkey),
            Err(e) => {
                error!(error = %e, "Failed to fetch active validators");
                false
            }
        }
    }

    fn build_bid_message(&self, bid: &MinerBid) -> String {
        // TODO: Canonicalize GPU category casing across miner/validator implementations.
        format!(
            "{}|{}|{:.8}|{}|{}|{}",
            bid.miner_hotkey.trim(),
            bid.gpu_category.trim(),
            bid.bid_per_hour,
            bid.gpu_count,
            bid.timestamp,
            bid.nonce.trim()
        )
    }

    #[allow(clippy::result_large_err)]
    fn sign_bid(&self, mut bid: MinerBid) -> Result<MinerBid, Status> {
        if bid.nonce.trim().is_empty() {
            bid.nonce = generate_nonce();
        }
        let message = self.build_bid_message(&bid);
        let signature_hex = self
            .bittensor_service
            .sign_data(message.as_bytes())
            .map_err(|e| Status::internal(format!("Failed to sign bid: {e}")))?;
        let signature_bytes = hex::decode(signature_hex)
            .map_err(|e| Status::internal(format!("Invalid signature hex: {e}")))?;
        bid.signature = signature_bytes;
        Ok(bid)
    }

    #[allow(clippy::result_large_err)]
    pub fn create_signed_bid(
        &self,
        gpu_category: String,
        bid_per_hour: f64,
        gpu_count: u32,
        attestation: Vec<u8>,
        timestamp: i64,
        nonce: Option<String>,
    ) -> Result<MinerBid, Status> {
        let bid = MinerBid {
            miner_hotkey: self.miner_hotkey.clone(),
            gpu_category,
            bid_per_hour,
            gpu_count,
            attestation,
            timestamp,
            nonce: nonce.unwrap_or_default(),
            signature: Vec::new(),
        };
        self.sign_bid(bid)
    }

    async fn forward_bid_to_validator(&self, bid: MinerBid) -> Result<SubmitBidResponse, Status> {
        let bid = if bid.signature.is_empty() {
            self.sign_bid(bid)?
        } else {
            bid
        };
        let endpoint = self
            .config
            .validator_bid_endpoint
            .clone()
            .ok_or_else(|| Status::failed_precondition("validator_bid_endpoint is not set"))?;

        let mut client =
            basilica_protocol::miner_discovery::miner_discovery_client::MinerDiscoveryClient::connect(
                endpoint.clone(),
            )
            .await
            .map_err(|e| Status::unavailable(format!("Failed to connect to validator: {e}")))?;

        let response = client
            .submit_bid(Request::new(SubmitBidRequest { bid: Some(bid) }))
            .await
            .map_err(|e| Status::internal(format!("Failed to submit bid: {e}")))?;

        Ok(response.into_inner())
    }
}

/// Generate a secure session token
fn generate_session_token() -> String {
    const TOKEN_LENGTH: usize = 32;
    let mut rng = rand::thread_rng();
    let token: Vec<u8> = (0..TOKEN_LENGTH).map(|_| rng.gen()).collect();
    hex::encode(token)
}

/// gRPC service implementation for MinerDiscovery
#[derive(Clone)]
pub struct MinerDiscoveryService {
    server: ValidatorCommsServer,
    bittensor_service: Arc<dyn BittensorServiceApi>,
}

#[tonic::async_trait]
impl MinerDiscovery for MinerDiscoveryService {
    /// Authenticate a validator using Bittensor signature
    async fn authenticate_validator(
        &self,
        request: Request<ValidatorAuthRequest>,
    ) -> Result<Response<MinerAuthResponse>, Status> {
        let auth_request = request.into_inner();

        if auth_request.target_miner_hotkey.trim().is_empty() {
            return Err(Status::invalid_argument("target_miner_hotkey is required"));
        }

        debug!(
            "Received authentication request from validator: {} for target miner: {}",
            auth_request.validator_hotkey, auth_request.target_miner_hotkey
        );

        // Verify target miner hotkey matches ours
        let our_hotkey = self.bittensor_service.get_account_id();
        if auth_request.target_miner_hotkey != our_hotkey {
            warn!(
                "Authentication request intended for different miner. Target: {}, Our hotkey: {}",
                auth_request.target_miner_hotkey, our_hotkey
            );
            return Err(Status::permission_denied(
                "Authentication request not intended for this miner",
            ));
        }
        debug!("Target miner hotkey matches our hotkey");

        // Verify the signature if enabled
        let validator_hotkey = Hotkey::new(auth_request.validator_hotkey.clone())
            .map_err(|e| Status::invalid_argument(format!("Invalid hotkey: {e}")))?;

        if self.server.security_config.verify_signatures {
            // Extract timestamp if provided
            let timestamp_secs = auth_request
                .timestamp
                .as_ref()
                .and_then(|t| t.value.as_ref())
                .map(|pt| pt.seconds)
                .unwrap_or(0);

            // Canonical payload with prefix and timestamp
            const AUTH_PREFIX: &str = "BASILICA_AUTH_V1";
            let canonical_payload = format!(
                "{}:{}:{}:{}",
                AUTH_PREFIX, auth_request.nonce, auth_request.target_miner_hotkey, timestamp_secs
            );

            // Verify signature
            use basilica_common::crypto::verify_bittensor_signature;
            match verify_bittensor_signature(
                &validator_hotkey,
                &auth_request.signature,
                canonical_payload.as_bytes(),
            ) {
                Ok(()) => {
                    debug!("Signature verification successful");
                }
                Err(e) => {
                    warn!("Signature verification failed for validator: {}", e);
                    return Err(Status::unauthenticated("Invalid signature"));
                }
            }

            // Check timestamp freshness (5 minutes)
            if timestamp_secs > 0 {
                let current_time = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_secs() as i64;
                let request_time = timestamp_secs;
                let time_diff = (current_time - request_time).abs();

                if time_diff > 300 {
                    warn!(
                        "Authentication request timestamp too old: {} seconds",
                        time_diff
                    );
                    return Err(Status::unauthenticated("Request timestamp too old"));
                }
            }
        }

        // Check if validator is authorized
        if !self
            .server
            .is_validator_authorized(&auth_request.validator_hotkey)
            .await
        {
            warn!(
                "Validator {} is not authorized",
                auth_request.validator_hotkey
            );
            return Err(Status::permission_denied("Validator not authorized"));
        }

        // Store authenticated validator
        let mut validators = self.server.authenticated_validators.write().await;
        validators.insert(
            auth_request.validator_hotkey.clone(),
            auth_request.nonce.clone(),
        );

        info!(
            "Successfully authenticated validator: {}",
            auth_request.validator_hotkey
        );

        // Generate session token for validator
        let session_token = generate_session_token();

        // Sign the response with miner's hotkey
        // Generate a fresh nonce for the response (security best practice)
        let response_nonce = uuid::Uuid::new_v4().to_string();
        let miner_hotkey = self.bittensor_service.get_account_id();

        // Create canonical response payload for signing
        let canonical_response = format!(
            "MINER_AUTH_RESPONSE:{}:{}:{}",
            auth_request.validator_hotkey, response_nonce, session_token
        );

        // Sign with miner's hotkey
        let (miner_hotkey, miner_signature, response_nonce) = match self
            .bittensor_service
            .sign_data(canonical_response.as_bytes())
        {
            Ok(sig) => (miner_hotkey, sig, response_nonce),
            Err(e) => {
                warn!("Failed to sign response: {}", e);
                (String::new(), String::new(), String::new())
            }
        };

        Ok(Response::new(MinerAuthResponse {
            authenticated: true,
            session_token,
            expires_at: Some(basilica_protocol::basilca::common::v1::Timestamp {
                value: Some(prost_types::Timestamp {
                    seconds: (std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap()
                        .as_secs()
                        + 3600) as i64,
                    nanos: 0,
                }),
            }),
            error: None,
            miner_hotkey,
            miner_signature,
            response_nonce,
        }))
    }

    /// Discover available nodes for validator
    async fn discover_nodes(
        &self,
        request: Request<DiscoverNodesRequest>,
    ) -> Result<Response<ListNodeConnectionDetailsResponse>, Status> {
        let discover_request = request.into_inner();

        // Verify the validator is authenticated
        let validators = self.server.authenticated_validators.read().await;
        if !validators.contains_key(&discover_request.validator_hotkey) {
            return Err(Status::unauthenticated(
                "Validator must authenticate before discovering nodes",
            ));
        }

        // Verify the validator is providing an SSH public key
        if discover_request.validator_public_key.is_empty() {
            return Err(Status::invalid_argument(
                "Validator must provide SSH public key",
            ));
        }

        debug!(
            "Validator {} discovering nodes with SSH key",
            discover_request.validator_hotkey
        );

        // Get the currently assigned validator from NodeManager (single source of truth)
        let assigned_validator = self.server.node_manager.get_assigned_validator().await;

        // Check if the requesting validator is the assigned validator
        if assigned_validator.as_deref() != Some(&discover_request.validator_hotkey) {
            info!(
                "Validator {} is not the assigned validator; returning empty node list",
                discover_request.validator_hotkey
            );
            return Ok(Response::new(ListNodeConnectionDetailsResponse {
                nodes: vec![],
            }));
        }

        // Deploy SSH keys to all nodes (exclusive access, removes old validator keys)
        if let Err(e) = self
            .server
            .node_manager
            .deploy_validator_keys(
                &discover_request.validator_hotkey,
                &discover_request.validator_public_key,
            )
            .await
        {
            error!("Failed to deploy validator SSH keys: {}", e);
            return Err(Status::internal(format!("Failed to deploy SSH keys: {e}")));
        }

        // Get all nodes and return them to the validator
        let nodes = match self.server.node_manager.list_nodes().await {
            Ok(nodes) => nodes,
            Err(e) => {
                error!("Failed to list nodes: {}", e);
                return Err(Status::internal(format!("Failed to list nodes: {e}")));
            }
        };

        // Convert to protocol format
        let node_details: Vec<basilica_protocol::miner_discovery::NodeConnectionDetails> = nodes
            .into_iter()
            .map(|registered_node| {
                // Convert hourly rate from dollars to cents for network transmission
                let hourly_rate_cents =
                    (registered_node.config.hourly_rate_per_gpu * 100.0).round() as u32;

                basilica_protocol::miner_discovery::NodeConnectionDetails {
                    node_id: registered_node.node_id,
                    host: registered_node.config.host,
                    port: registered_node.config.port.to_string(),
                    username: registered_node.config.username,
                    additional_opts: registered_node.config.additional_opts.unwrap_or_default(),
                    gpu_spec: None, // Validators discover GPU specs via SSH
                    status: "available".to_string(),
                    hourly_rate_cents,
                }
            })
            .collect();

        info!(
            "Returning {} nodes to validator {}",
            node_details.len(),
            discover_request.validator_hotkey
        );

        Ok(Response::new(ListNodeConnectionDetailsResponse {
            nodes: node_details,
        }))
    }

    async fn submit_bid(
        &self,
        request: Request<SubmitBidRequest>,
    ) -> Result<Response<SubmitBidResponse>, Status> {
        let bid = request
            .into_inner()
            .bid
            .ok_or_else(|| Status::invalid_argument("bid is required"))?;

        validate_bid(&bid, &self.server.miner_hotkey)?;
        let response = self.server.forward_bid_to_validator(bid).await?;
        Ok(Response::new(response))
    }
}

#[allow(clippy::result_large_err)]
fn validate_bid(bid: &MinerBid, expected_hotkey: &str) -> Result<(), Status> {
    if bid.miner_hotkey.trim().is_empty() {
        return Err(Status::invalid_argument("miner_hotkey is required"));
    }
    if bid.miner_hotkey != expected_hotkey {
        return Err(Status::permission_denied(
            "miner_hotkey does not match this miner",
        ));
    }
    if bid.gpu_category.trim().is_empty() {
        return Err(Status::invalid_argument("gpu_category is required"));
    }
    if bid.bid_per_hour <= 0.0 {
        return Err(Status::invalid_argument(
            "bid_per_hour must be greater than 0",
        ));
    }
    if bid.gpu_count == 0 {
        return Err(Status::invalid_argument("gpu_count must be greater than 0"));
    }
    if bid.signature.is_empty() {
        return Err(Status::invalid_argument("signature is required"));
    }
    if bid.nonce.trim().is_empty() {
        return Err(Status::invalid_argument("nonce is required"));
    }
    let hotkey = Hotkey::new(bid.miner_hotkey.clone())
        .map_err(|e| Status::invalid_argument(e.to_string()))?;
    let message = format!(
        "{}|{}|{:.8}|{}|{}|{}",
        bid.miner_hotkey.trim(),
        bid.gpu_category.trim(),
        bid.bid_per_hour,
        bid.gpu_count,
        bid.timestamp,
        bid.nonce.trim()
    );
    verify_signature_bittensor(&hotkey, &bid.signature, message.as_bytes())
        .map_err(|e| Status::permission_denied(e.to_string()))?;
    Ok(())
}

fn generate_nonce() -> String {
    let mut rng = rand::thread_rng();
    let bytes: Vec<u8> = (0..16).map(|_| rng.gen()).collect();
    hex::encode(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{NodeSshConfig, SecurityConfig, ValidatorCommsConfig};
    use crate::node_manager::NodeManager;
    use basilica_common::crypto::wallet::{
        generate_sr25519_wallet, sign_with_sr25519, sr25519_pair_from_mnemonic,
    };
    use basilica_protocol::miner_discovery::miner_discovery_server::{
        MinerDiscovery, MinerDiscoveryServer,
    };
    use bittensor::crypto::sr25519;
    use std::net::TcpListener;

    struct TestValidatorDiscovery;

    #[async_trait::async_trait]
    impl ValidatorDiscoveryApi for TestValidatorDiscovery {
        async fn get_active_validators(&self) -> Result<Vec<ValidatorInfo>> {
            Ok(Vec::new())
        }
    }

    #[derive(Clone)]
    struct TestBittensorService {
        hotkey: String,
        pair: sr25519::Pair,
    }

    impl BittensorServiceApi for TestBittensorService {
        fn get_account_id(&self) -> String {
            self.hotkey.clone()
        }

        fn sign_data(&self, _data: &[u8]) -> Result<String> {
            Ok(sign_with_sr25519(&self.pair, _data))
        }
    }

    fn make_signed_bid(server: &ValidatorCommsServer) -> MinerBid {
        server
            .create_signed_bid("H100".to_string(), 2.0, 2, vec![1, 2, 3], 123, None)
            .unwrap()
    }

    fn build_test_bittensor() -> (Arc<dyn BittensorServiceApi>, String) {
        let wallet = generate_sr25519_wallet(42).unwrap();
        let pair = sr25519_pair_from_mnemonic(&wallet.mnemonic).unwrap();
        let hotkey = wallet.address;
        (
            Arc::new(TestBittensorService {
                hotkey: hotkey.clone(),
                pair,
            }),
            hotkey,
        )
    }

    async fn build_server(
        config: ValidatorCommsConfig,
    ) -> (ValidatorCommsServer, Arc<dyn BittensorServiceApi>) {
        let node_manager = Arc::new(NodeManager::new(NodeSshConfig::default()));
        let validator_discovery: Arc<dyn ValidatorDiscoveryApi> = Arc::new(TestValidatorDiscovery);
        let (bittensor_service, hotkey) = build_test_bittensor();

        let server = ValidatorCommsServer::new(
            config,
            SecurityConfig::default(),
            node_manager,
            validator_discovery,
            bittensor_service.clone(),
        )
        .await
        .unwrap();

        let server = ValidatorCommsServer {
            miner_hotkey: hotkey,
            ..server
        };

        (server, bittensor_service)
    }

    #[derive(Default)]
    struct MockValidatorBidService;

    #[tonic::async_trait]
    impl MinerDiscovery for MockValidatorBidService {
        async fn authenticate_validator(
            &self,
            _request: Request<ValidatorAuthRequest>,
        ) -> Result<Response<MinerAuthResponse>, Status> {
            Err(Status::unimplemented("authenticate_validator"))
        }

        async fn discover_nodes(
            &self,
            _request: Request<DiscoverNodesRequest>,
        ) -> Result<Response<ListNodeConnectionDetailsResponse>, Status> {
            Err(Status::unimplemented("discover_nodes"))
        }

        async fn submit_bid(
            &self,
            _request: Request<SubmitBidRequest>,
        ) -> Result<Response<SubmitBidResponse>, Status> {
            Ok(Response::new(SubmitBidResponse {
                accepted: true,
                error_message: String::new(),
                epoch_id: "test-epoch".to_string(),
            }))
        }
    }

    async fn start_mock_validator_server() -> String {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);
        let service = MockValidatorBidService;

        tokio::spawn(async move {
            tonic::transport::Server::builder()
                .add_service(MinerDiscoveryServer::new(service))
                .serve(addr)
                .await
                .unwrap();
        });

        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        format!("http://{}", addr)
    }

    #[tokio::test]
    async fn test_validate_bid_rejects_missing_fields() {
        let (server, _) = build_server(ValidatorCommsConfig::default()).await;
        let expected_hotkey = server.miner_hotkey.clone();
        let mut bid = make_signed_bid(&server);
        bid.miner_hotkey = "".to_string();
        assert_eq!(
            validate_bid(&bid, &expected_hotkey).unwrap_err().code(),
            tonic::Code::InvalidArgument
        );

        bid = make_signed_bid(&server);
        bid.gpu_category = "".to_string();
        assert_eq!(
            validate_bid(&bid, &expected_hotkey).unwrap_err().code(),
            tonic::Code::InvalidArgument
        );

        bid = make_signed_bid(&server);
        bid.bid_per_hour = 0.0;
        assert_eq!(
            validate_bid(&bid, &expected_hotkey).unwrap_err().code(),
            tonic::Code::InvalidArgument
        );

        bid = make_signed_bid(&server);
        bid.gpu_count = 0;
        assert_eq!(
            validate_bid(&bid, &expected_hotkey).unwrap_err().code(),
            tonic::Code::InvalidArgument
        );

        bid = make_signed_bid(&server);
        bid.signature = vec![];
        assert_eq!(
            validate_bid(&bid, &expected_hotkey).unwrap_err().code(),
            tonic::Code::InvalidArgument
        );
    }

    #[tokio::test]
    async fn test_validate_bid_rejects_wrong_hotkey() {
        let (server, _) = build_server(ValidatorCommsConfig::default()).await;
        let mut bid = make_signed_bid(&server);
        bid.miner_hotkey = "other_hotkey".to_string();
        assert_eq!(
            validate_bid(&bid, &server.miner_hotkey).unwrap_err().code(),
            tonic::Code::PermissionDenied
        );
    }

    #[tokio::test]
    async fn test_submit_bid_success() {
        let endpoint = start_mock_validator_server().await;
        let (server, bittensor_service) = build_server(ValidatorCommsConfig {
            validator_bid_endpoint: Some(endpoint),
            ..ValidatorCommsConfig::default()
        })
        .await;
        let service = MinerDiscoveryService {
            server,
            bittensor_service,
        };

        let request = Request::new(SubmitBidRequest {
            bid: Some(make_signed_bid(&service.server)),
        });
        let response = service.submit_bid(request).await.unwrap().into_inner();

        assert!(response.accepted);
        assert_eq!(response.epoch_id, "test-epoch");
    }

    #[tokio::test]
    async fn test_submit_bid_rejects_invalid_bid() {
        let (server, bittensor_service) = build_server(ValidatorCommsConfig::default()).await;
        let service = MinerDiscoveryService {
            server,
            bittensor_service,
        };

        let mut bid = make_signed_bid(&service.server);
        bid.bid_per_hour = 0.0;

        let request = Request::new(SubmitBidRequest { bid: Some(bid) });
        let result = service.submit_bid(request).await;
        assert!(result.is_err());
        assert_eq!(result.unwrap_err().code(), tonic::Code::InvalidArgument);
    }
}
