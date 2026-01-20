use std::sync::Arc;

use anyhow::Result;
use basilica_common::config::GrpcServerConfig;
use basilica_common::crypto::verify_signature_bittensor;
use basilica_common::identity::Hotkey;
use basilica_protocol::miner_discovery::{
    miner_discovery_server::{MinerDiscovery, MinerDiscoveryServer},
    DiscoverNodesRequest, ListNodeConnectionDetailsResponse, MinerAuthResponse, SubmitBidRequest,
    SubmitBidResponse, ValidatorAuthRequest,
};
use chrono::{DateTime, TimeZone, Utc};
use tonic::{transport::Server, Request, Response, Status};
use tracing::{info, warn};
use uuid::Uuid;

use crate::config::auction::AuctionConfig;
use crate::persistence::bids::{AuctionEpoch, BidRepository, MinerBidRecord};
use crate::persistence::SimplePersistence;

#[derive(Clone)]
pub struct BidService {
    persistence: Arc<SimplePersistence>,
    auction_config: AuctionConfig,
}

impl BidService {
    pub fn new(persistence: Arc<SimplePersistence>, auction_config: AuctionConfig) -> Self {
        Self {
            persistence,
            auction_config,
        }
    }

    async fn get_or_create_active_epoch(&self, repo: &BidRepository) -> Result<AuctionEpoch> {
        if let Some(epoch) = repo.get_active_epoch().await? {
            return Ok(epoch);
        }

        // TODO: Snapshot baseline prices into baseline_prices_json once pricing is wired here.
        let epoch = AuctionEpoch {
            id: format!("epoch-{}", Utc::now().timestamp_millis()),
            start_block: 0,
            end_block: None,
            baseline_prices_json: "{}".to_string(),
            status: "active".to_string(),
            created_at: Utc::now(),
        };
        repo.create_epoch(&epoch).await?;
        Ok(epoch)
    }

    fn validate_bid_fields(&self, bid: &basilica_protocol::miner_discovery::MinerBid) -> Result<()> {
        if bid.miner_hotkey.trim().is_empty() {
            anyhow::bail!("miner_hotkey is required");
        }
        if bid.gpu_category.trim().is_empty() {
            anyhow::bail!("gpu_category is required");
        }
        if bid.bid_per_hour <= 0.0 {
            anyhow::bail!("bid_per_hour must be greater than 0");
        }
        if bid.gpu_count == 0 {
            anyhow::bail!("gpu_count must be greater than 0");
        }
        if bid.signature.is_empty() {
            anyhow::bail!("signature is required");
        }
        if bid.nonce.trim().is_empty() {
            anyhow::bail!("nonce is required");
        }
        Ok(())
    }

    fn build_bid_message(&self, bid: &basilica_protocol::miner_discovery::MinerBid) -> String {
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

    fn verify_bid_signature(&self, bid: &basilica_protocol::miner_discovery::MinerBid) -> Result<()> {
        let hotkey = Hotkey::new(bid.miner_hotkey.clone())
            .map_err(|e| anyhow::anyhow!("invalid miner_hotkey: {e}"))?;
        let message = self.build_bid_message(bid);

        // TODO: Add replay protection (timestamp window + nonce) once bid nonces are defined.
        verify_signature_bittensor(&hotkey, &bid.signature, message.as_bytes())
            .map_err(|e| anyhow::anyhow!("signature verification failed: {e}"))?;
        Ok(())
    }

    fn parse_bid_timestamp(&self, timestamp: i64) -> Result<DateTime<Utc>> {
        if timestamp <= 0 {
            anyhow::bail!("timestamp must be positive");
        }
        let dt = if timestamp > 1_000_000_000_000 {
            let secs = timestamp / 1000;
            Utc.timestamp_opt(secs, 0)
        } else {
            Utc.timestamp_opt(timestamp, 0)
        }
        .single()
        .ok_or_else(|| anyhow::anyhow!("invalid timestamp"))?;
        Ok(dt)
    }

    async fn resolve_miner_uid(&self, miner_hotkey: &str) -> Result<i64> {
        let miner_id = self
            .persistence
            .check_miner_by_hotkey(miner_hotkey)
            .await?
            .ok_or_else(|| anyhow::anyhow!("unknown miner_hotkey"))?;

        let miner_uid = miner_id
            .strip_prefix("miner_")
            .and_then(|uid| uid.parse::<i64>().ok())
            .ok_or_else(|| anyhow::anyhow!("invalid miner_id format"))?;

        Ok(miner_uid)
    }

    async fn expire_old_bids(&self, repo: &BidRepository) -> Result<()> {
        let cutoff = Utc::now() - chrono::Duration::seconds(self.auction_config.bid_validity_secs as i64);
        repo.expire_old_bids(cutoff).await?;
        Ok(())
    }
}

#[tonic::async_trait]
impl MinerDiscovery for BidService {
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
        request: Request<SubmitBidRequest>,
    ) -> Result<Response<SubmitBidResponse>, Status> {
        let bid = request
            .into_inner()
            .bid
            .ok_or_else(|| Status::invalid_argument("bid is required"))?;

        self.validate_bid_fields(&bid)
            .map_err(|e| Status::invalid_argument(e.to_string()))?;
        self.verify_bid_signature(&bid)
            .map_err(|e| Status::permission_denied(e.to_string()))?;

        let submitted_at = self
            .parse_bid_timestamp(bid.timestamp)
            .map_err(|e| Status::invalid_argument(e.to_string()))?;
        let now = Utc::now();
        let max_skew_secs = self.auction_config.bid_validity_secs as i64;
        if (now - submitted_at).num_seconds().abs() > max_skew_secs {
            return Err(Status::invalid_argument("timestamp outside allowed window"));
        }

        let miner_uid = self
            .resolve_miner_uid(&bid.miner_hotkey)
            .await
            .map_err(|e| Status::not_found(e.to_string()))?;
        let miner_id = format!("miner_{}", miner_uid);

        let available_nodes = self
            .persistence
            .get_available_nodes_for_miner(
                &miner_id,
                &bid.gpu_category,
                bid.gpu_count as u32,
                self.auction_config.bid_node_freshness_secs,
            )
            .await
            .map_err(|e| Status::internal(format!("failed to check miner capacity: {e}")))?;

        if available_nodes.is_empty() {
            return Err(Status::failed_precondition(
                "miner has no available nodes for the requested category/count",
            ));
        }

        let repo = BidRepository::new(self.persistence.pool().clone());
        if let Err(err) = self.expire_old_bids(&repo).await {
            warn!("Failed to expire old bids: {}", err);
        }

        let replay_cutoff = now - chrono::Duration::seconds(max_skew_secs);
        if repo
            .nonce_exists(&bid.miner_hotkey, &bid.nonce, replay_cutoff)
            .await
            .map_err(|e| Status::internal(format!("failed to check nonce: {e}")))? {
            return Err(Status::already_exists("nonce already used"));
        }

        let epoch = self
            .get_or_create_active_epoch(&repo)
            .await
            .map_err(|e| Status::internal(e.to_string()))?;

        let record = MinerBidRecord {
            id: format!("bid-{}", Uuid::new_v4()),
            miner_hotkey: bid.miner_hotkey.clone(),
            miner_uid,
            gpu_category: bid.gpu_category.clone(),
            bid_per_hour: bid.bid_per_hour,
            gpu_count: bid.gpu_count as i64,
            attestation: if bid.attestation.is_empty() {
                None
            } else {
                Some(bid.attestation.clone())
            },
            signature: bid.signature.clone(),
            nonce: bid.nonce.clone(),
            submitted_at,
            epoch_id: epoch.id.clone(),
            is_valid: true,
        };

        repo.insert_bid(&record)
            .await
            .map_err(|e| Status::internal(format!("failed to insert bid: {e}")))?;
        repo.insert_bid_nodes(
            &record.id,
            &miner_id,
            &record.gpu_category,
            record.gpu_count,
            &available_nodes,
            record.submitted_at,
        )
        .await
        .map_err(|e| Status::internal(format!("failed to insert bid nodes: {e}")))?;

        // TODO: Add replay protection with persistent nonce eviction policy.
        // TODO: Consider sharding bids per node for multi-node rentals.
        info!(
            miner_hotkey = %bid.miner_hotkey,
            miner_uid = miner_uid,
            gpu_category = %bid.gpu_category,
            bid_per_hour = bid.bid_per_hour,
            "Accepted miner bid"
        );

        Ok(Response::new(SubmitBidResponse {
            accepted: true,
            error_message: String::new(),
            epoch_id: epoch.id,
        }))
    }
}

pub async fn start_bid_server(
    config: GrpcServerConfig,
    persistence: Arc<SimplePersistence>,
    auction_config: AuctionConfig,
) -> Result<()> {
    let service = BidService::new(persistence, auction_config);
    let addr = config
        .listen_address
        .parse()
        .map_err(|e| anyhow::anyhow!("invalid gRPC listen address: {}", e))?;

    info!(address = %config.listen_address, "Starting bid gRPC server");

    Server::builder()
        .add_service(MinerDiscoveryServer::new(service))
        .serve(addr)
        .await
        .map_err(|e| anyhow::anyhow!("bid gRPC server failed: {}", e))?;

    Ok(())
}

