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
use tonic_health::server::health_reporter;
use tracing::{info, warn};
use uuid::Uuid;

use crate::collateral::{CollateralManager, CollateralState, CollateralStatus};
use crate::config::auction::AuctionConfig;
use crate::persistence::bids::{AuctionEpoch, BidRepository, MinerBidRecord};
use crate::persistence::SimplePersistence;

fn status_rank(state: &CollateralState) -> u8 {
    match state {
        CollateralState::Excluded { .. } => 4,
        CollateralState::Undercollateralized { .. } => 3,
        CollateralState::Warning { .. } => 2,
        CollateralState::Sufficient { .. } => 1,
        CollateralState::Unknown { .. } => 0,
    }
}

fn select_worst_status(
    current: Option<CollateralStatus>,
    state: CollateralState,
    next: CollateralStatus,
) -> CollateralStatus {
    if let Some(existing) = current {
        // Compare by rank embedded in status string
        let existing_rank = status_rank_from_status(&existing.status);
        let next_rank = status_rank(&state);
        if next_rank > existing_rank {
            next
        } else {
            existing
        }
    } else {
        next
    }
}

fn status_rank_from_status(status: &str) -> u8 {
    match status {
        "excluded" => 4,
        "undercollateralized" => 3,
        "warning" => 2,
        "sufficient" => 1,
        _ => 0,
    }
}

fn status_to_proto(
    status: CollateralStatus,
) -> basilica_protocol::miner_discovery::CollateralStatus {
    basilica_protocol::miner_discovery::CollateralStatus {
        current_alpha: status.current_alpha,
        current_usd_value: status.current_usd_value,
        minimum_usd_required: status.minimum_usd_required,
        status: status.status,
        grace_period_remaining: status
            .grace_period_remaining
            .map(format_duration)
            .unwrap_or_default(),
        action_required: status.action_required.unwrap_or_default(),
        alpha_usd_price: status.alpha_usd_price.unwrap_or_default(),
        price_stale: status.price_stale,
    }
}

fn format_duration(duration: chrono::Duration) -> String {
    let total_seconds = duration.num_seconds().max(0);
    let hours = total_seconds / 3600;
    let minutes = (total_seconds % 3600) / 60;
    if hours > 0 {
        format!("{}h {}m", hours, minutes)
    } else {
        format!("{}m", minutes)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_status(status: &str) -> CollateralStatus {
        CollateralStatus {
            current_alpha: 10.0,
            current_usd_value: 10.0,
            minimum_usd_required: 5.0,
            status: status.to_string(),
            grace_period_remaining: None,
            action_required: None,
            alpha_usd_price: Some(1.0),
            price_stale: false,
        }
    }

    #[test]
    fn test_format_duration_minutes_only() {
        let d = chrono::Duration::minutes(42);
        assert_eq!(format_duration(d), "42m");
    }

    #[test]
    fn test_format_duration_hours_minutes() {
        let d = chrono::Duration::minutes(125);
        assert_eq!(format_duration(d), "2h 5m");
    }

    #[test]
    fn test_select_worst_status_prefers_excluded() {
        let current = make_status("warning");
        let next = make_status("excluded");
        let selected = select_worst_status(
            Some(current),
            CollateralState::Excluded {
                current_usd: 0.0,
                minimum_usd: 10.0,
                reason: "expired".to_string(),
            },
            next.clone(),
        );
        assert_eq!(selected.status, "excluded");
    }
}

fn canonicalize_gpu_category(category: &str) -> String {
    category.trim().to_uppercase()
}

#[derive(Clone)]
pub struct BidService {
    persistence: Arc<SimplePersistence>,
    auction_config: AuctionConfig,
    collateral_manager: Option<Arc<CollateralManager>>,
}

impl BidService {
    pub fn new(
        persistence: Arc<SimplePersistence>,
        auction_config: AuctionConfig,
        collateral_manager: Option<Arc<CollateralManager>>,
    ) -> Self {
        Self {
            persistence,
            auction_config,
            collateral_manager,
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

    fn validate_bid_fields(
        &self,
        bid: &basilica_protocol::miner_discovery::MinerBid,
    ) -> Result<()> {
        const MAX_HOTKEY_LEN: usize = 64;
        const MAX_CATEGORY_LEN: usize = 32;
        const MAX_NONCE_LEN: usize = 128;
        const MAX_SIGNATURE_LEN: usize = 512;
        const MAX_ATTESTATION_LEN: usize = 10_000;

        if bid.miner_hotkey.trim().is_empty() {
            anyhow::bail!("miner_hotkey is required");
        }
        if bid.miner_hotkey.len() > MAX_HOTKEY_LEN {
            anyhow::bail!("miner_hotkey too long");
        }
        if bid.gpu_category.trim().is_empty() {
            anyhow::bail!("gpu_category is required");
        }
        if bid.gpu_category.len() > MAX_CATEGORY_LEN {
            anyhow::bail!("gpu_category too long");
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
        if bid.signature.len() > MAX_SIGNATURE_LEN {
            anyhow::bail!("signature too long");
        }
        if bid.nonce.trim().is_empty() {
            anyhow::bail!("nonce is required");
        }
        if bid.nonce.len() > MAX_NONCE_LEN {
            anyhow::bail!("nonce too long");
        }
        if bid.attestation.len() > MAX_ATTESTATION_LEN {
            anyhow::bail!("attestation too long");
        }
        Ok(())
    }

    fn build_bid_message(&self, bid: &basilica_protocol::miner_discovery::MinerBid) -> String {
        let gpu_category = canonicalize_gpu_category(&bid.gpu_category);
        format!(
            "{}|{}|{:.8}|{}|{}|{}",
            bid.miner_hotkey.trim(),
            gpu_category,
            bid.bid_per_hour,
            bid.gpu_count,
            bid.timestamp,
            bid.nonce.trim()
        )
    }

    fn verify_bid_signature(
        &self,
        bid: &basilica_protocol::miner_discovery::MinerBid,
    ) -> Result<()> {
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
        let cutoff =
            Utc::now() - chrono::Duration::seconds(self.auction_config.bid_validity_secs as i64);
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
        let canonical_category = canonicalize_gpu_category(&bid.gpu_category);

        let available_nodes = self
            .persistence
            .get_available_nodes_for_miner(
                &miner_id,
                &canonical_category,
                bid.gpu_count,
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

        let nonce_retention_secs = self.auction_config.bid_validity_secs.saturating_mul(2) as i64;
        let replay_cutoff = now - chrono::Duration::seconds(nonce_retention_secs);
        if let Err(err) = repo.delete_bids_before(replay_cutoff).await {
            warn!("Failed to prune old bids: {}", err);
        }
        let epoch = self
            .get_or_create_active_epoch(&repo)
            .await
            .map_err(|e| Status::internal(e.to_string()))?;

        let record = MinerBidRecord {
            id: format!("bid-{}", Uuid::new_v4()),
            miner_hotkey: bid.miner_hotkey.clone(),
            miner_uid,
            gpu_category: canonical_category.clone(),
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

        let mut tx = repo
            .pool()
            .begin()
            .await
            .map_err(|e| Status::internal(format!("failed to begin transaction: {e}")))?;

        if repo
            .nonce_exists_tx(&mut tx, &bid.miner_hotkey, &bid.nonce, replay_cutoff)
            .await
            .map_err(|e| Status::internal(format!("failed to check nonce: {e}")))?
        {
            return Err(Status::already_exists("nonce already used"));
        }

        repo.insert_bid_tx(&mut tx, &record)
            .await
            .map_err(|e| Status::internal(format!("failed to insert bid: {e}")))?;
        repo.insert_bid_nodes_tx(
            &mut tx,
            &record.id,
            &miner_id,
            &record.gpu_category,
            record.gpu_count,
            &available_nodes,
            record.submitted_at,
        )
        .await
        .map_err(|e| Status::internal(format!("failed to insert bid nodes: {e}")))?;

        tx.commit()
            .await
            .map_err(|e| Status::internal(format!("failed to commit transaction: {e}")))?;

        // TODO: Add replay protection with persistent nonce eviction policy.
        // TODO: Consider sharding bids per node for multi-node rentals.
        info!(
            miner_hotkey = %bid.miner_hotkey,
            miner_uid = miner_uid,
            gpu_category = %canonical_category,
            bid_per_hour = bid.bid_per_hour,
            "Accepted miner bid"
        );

        let collateral_status = match &self.collateral_manager {
            Some(manager) => {
                let mut worst_status = None;
                for node_id in available_nodes.iter() {
                    let (state, status) = manager
                        .get_collateral_status(
                            &bid.miner_hotkey,
                            node_id,
                            &canonical_category,
                            bid.gpu_count,
                        )
                        .await
                        .map_err(|e| Status::internal(format!("collateral status error: {e}")))?;
                    worst_status = Some(select_worst_status(worst_status, state, status));
                }
                worst_status.map(status_to_proto)
            }
            None => None,
        };

        Ok(Response::new(SubmitBidResponse {
            accepted: true,
            error_message: String::new(),
            epoch_id: epoch.id,
            collateral_status,
        }))
    }
}

pub async fn start_bid_server(
    config: GrpcServerConfig,
    persistence: Arc<SimplePersistence>,
    auction_config: AuctionConfig,
    collateral_manager: Option<Arc<CollateralManager>>,
) -> Result<()> {
    let service = BidService::new(persistence, auction_config, collateral_manager);
    let addr = config
        .listen_address
        .parse()
        .map_err(|e| anyhow::anyhow!("invalid gRPC listen address: {}", e))?;

    let (mut health_reporter, health_service) = health_reporter();
    health_reporter
        .set_serving::<MinerDiscoveryServer<BidService>>()
        .await;

    info!(address = %config.listen_address, "Starting bid gRPC server");

    Server::builder()
        .add_service(health_service)
        .add_service(MinerDiscoveryServer::new(service))
        .serve(addr)
        .await
        .map_err(|e| anyhow::anyhow!("bid gRPC server failed: {}", e))?;

    Ok(())
}
