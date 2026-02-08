//! MinerRegistration gRPC service implementation.
//!
//! This service handles the miner→validator registration flow:
//! - RegisterBid: One-time registration of nodes with SSH details + pricing
//! - UpdateBid: Update bid price for existing registered node
//! - RemoveBid: Explicitly remove node(s) from availability
//! - HealthCheck: Lightweight periodic heartbeat to keep registrations active

use std::sync::Arc;

use anyhow::Result;
use basilica_common::config::GrpcServerConfig;
use basilica_common::crypto::verify_signature_bittensor;
use basilica_common::identity::Hotkey;
use basilica_common::types::GpuCategory;
use basilica_protocol::miner_discovery::{
    miner_registration_server::{MinerRegistration, MinerRegistrationServer},
    HealthCheckRequest, HealthCheckResponse, RegisterBidRequest, RegisterBidResponse,
    RemoveBidRequest, RemoveBidResponse, UpdateBidRequest, UpdateBidResponse,
};
use chrono::{TimeZone, Utc};
use tonic::{transport::Server, Request, Response, Status};
use tonic_health::server::health_reporter;
use tracing::info;
use uuid::Uuid;

use crate::collateral::{CollateralManager, CollateralState, CollateralStatus};
use crate::config::auction::AuctionConfig;
use crate::persistence::SimplePersistence;

/// Convert internal CollateralStatus to proto CollateralStatus
fn status_to_proto(
    status: CollateralStatus,
) -> basilica_protocol::miner_discovery::CollateralStatus {
    basilica_protocol::miner_discovery::CollateralStatus {
        current_alpha: status.current_alpha.to_f64().unwrap_or_default(),
        current_usd_value: status.current_usd_value.to_f64().unwrap_or_default(),
        minimum_usd_required: status.minimum_usd_required.to_f64().unwrap_or_default(),
        status: status.status,
        grace_period_remaining: status
            .grace_period_remaining
            .map(format_duration)
            .unwrap_or_default(),
        action_required: status.action_required.unwrap_or_default(),
        alpha_usd_price: status
            .alpha_usd_price
            .and_then(|price| price.to_f64())
            .unwrap_or_default(),
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

fn status_rank(state: &CollateralState) -> u8 {
    match state {
        CollateralState::Excluded { .. } => 4,
        CollateralState::Undercollateralized { .. } => 3,
        CollateralState::Warning { .. } => 2,
        CollateralState::Sufficient { .. } => 1,
        CollateralState::Unknown { .. } => 0,
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

fn select_worst_status(
    current: Option<CollateralStatus>,
    state: CollateralState,
    next: CollateralStatus,
) -> CollateralStatus {
    if let Some(existing) = current {
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

use rust_decimal::prelude::ToPrimitive;

#[derive(Clone)]
pub struct RegistrationService {
    persistence: Arc<SimplePersistence>,
    auction_config: AuctionConfig,
    collateral_manager: Option<Arc<CollateralManager>>,
    validator_ssh_public_key: String,
}

#[allow(clippy::result_large_err)]
impl RegistrationService {
    pub fn new(
        persistence: Arc<SimplePersistence>,
        auction_config: AuctionConfig,
        collateral_manager: Option<Arc<CollateralManager>>,
        validator_ssh_public_key: String,
    ) -> Self {
        Self {
            persistence,
            auction_config,
            collateral_manager,
            validator_ssh_public_key,
        }
    }

    /// Verify timestamp is within allowed window
    fn ensure_timestamp_freshness(&self, timestamp: i64) -> Result<(), Status> {
        let submitted_at = self.parse_timestamp(timestamp)?;
        let now = Utc::now();
        let max_skew_secs = self.auction_config.bid_validity_secs as i64;
        if (now - submitted_at).num_seconds().abs() > max_skew_secs {
            return Err(Status::invalid_argument("timestamp outside allowed window"));
        }
        Ok(())
    }

    fn parse_timestamp(&self, timestamp: i64) -> Result<chrono::DateTime<Utc>, Status> {
        if timestamp <= 0 {
            return Err(Status::invalid_argument("timestamp must be positive"));
        }
        let dt = if timestamp > 1_000_000_000_000 {
            let secs = timestamp / 1000;
            Utc.timestamp_opt(secs, 0)
        } else {
            Utc.timestamp_opt(timestamp, 0)
        }
        .single()
        .ok_or_else(|| Status::invalid_argument("invalid timestamp"))?;
        Ok(dt)
    }

    /// Resolve miner hotkey to miner_id (miner_UID format)
    async fn resolve_miner_id(&self, miner_hotkey: &str) -> Result<String, Status> {
        let miner_id = self
            .persistence
            .check_miner_by_hotkey(miner_hotkey)
            .await
            .map_err(|e| Status::internal(format!("database error: {e}")))?
            .ok_or_else(|| Status::not_found("unknown miner_hotkey"))?;
        Ok(miner_id)
    }

    /// Build the message to verify for RegisterBid signature
    fn build_register_bid_message(&self, req: &RegisterBidRequest) -> String {
        format!("{}|{}", req.miner_hotkey.trim(), req.timestamp)
    }

    /// Build the message to verify for UpdateBid signature
    fn build_update_bid_message(&self, req: &UpdateBidRequest) -> String {
        format!(
            "{}|{}|{}|{}",
            req.miner_hotkey.trim(),
            req.node_id.trim(),
            req.hourly_rate_cents,
            req.timestamp,
        )
    }

    /// Build the message to verify for RemoveBid signature
    fn build_remove_bid_message(&self, req: &RemoveBidRequest) -> String {
        let node_ids_str = req.node_ids.join(",");
        format!(
            "{}|{}|{}",
            req.miner_hotkey.trim(),
            node_ids_str,
            req.timestamp,
        )
    }

    /// Build the message to verify for HealthCheck signature
    fn build_health_check_message(&self, req: &HealthCheckRequest) -> String {
        let node_ids_str = req.node_ids.join(",");
        format!(
            "{}|{}|{}",
            req.miner_hotkey.trim(),
            node_ids_str,
            req.timestamp
        )
    }

    /// Verify a Bittensor signature
    fn verify_signature(
        &self,
        hotkey: &str,
        signature: &[u8],
        message: &str,
    ) -> Result<(), Status> {
        let hotkey = Hotkey::new(hotkey.to_string())
            .map_err(|e| Status::invalid_argument(format!("invalid hotkey: {e}")))?;
        verify_signature_bittensor(&hotkey, signature, message.as_bytes()).map_err(|e| {
            Status::permission_denied(format!("signature verification failed: {e}"))
        })?;
        Ok(())
    }
}

#[tonic::async_trait]
impl MinerRegistration for RegistrationService {
    async fn register_bid(
        &self,
        request: Request<RegisterBidRequest>,
    ) -> Result<Response<RegisterBidResponse>, Status> {
        let req = request.into_inner();

        // Validate required fields
        if req.miner_hotkey.trim().is_empty() {
            return Err(Status::invalid_argument("miner_hotkey is required"));
        }
        if req.nodes.is_empty() {
            return Err(Status::invalid_argument("at least one node is required"));
        }
        if req.signature.is_empty() {
            return Err(Status::invalid_argument("signature is required"));
        }

        // Verify timestamp freshness
        self.ensure_timestamp_freshness(req.timestamp)?;

        // Verify signature
        let message = self.build_register_bid_message(&req);
        self.verify_signature(&req.miner_hotkey, &req.signature, &message)?;

        // Resolve miner ID
        let miner_id = self.resolve_miner_id(&req.miner_hotkey).await?;

        // Generate registration ID
        let registration_id = format!("reg-{}", Uuid::new_v4());

        // Upsert each node
        let mut worst_collateral_status: Option<CollateralStatus> = None;
        for node in &req.nodes {
            // Validate node fields
            if node.node_id.trim().is_empty() {
                return Err(Status::invalid_argument("node_id is required"));
            }
            if node.host.trim().is_empty() {
                return Err(Status::invalid_argument("host is required"));
            }
            if node.port == 0 {
                return Err(Status::invalid_argument("port must be greater than 0"));
            }
            if node.username.trim().is_empty() {
                return Err(Status::invalid_argument("username is required"));
            }
            if node.gpu_category.trim().is_empty() {
                return Err(Status::invalid_argument("gpu_category is required"));
            }
            // Validate gpu_category is a known GPU type
            let gpu_cat: GpuCategory = node.gpu_category.parse().unwrap(); // Infallible
            if matches!(&gpu_cat, GpuCategory::Other(_)) {
                return Err(Status::invalid_argument(format!(
                    "GPU type '{}' is not supported",
                    node.gpu_category
                )));
            }
            if node.gpu_count == 0 {
                return Err(Status::invalid_argument("gpu_count must be greater than 0"));
            }
            if node.hourly_rate_cents == 0 {
                return Err(Status::invalid_argument(
                    "hourly_rate_cents must be greater than 0",
                ));
            }

            // Upsert node
            self.persistence
                .upsert_registered_node(
                    &miner_id,
                    &node.node_id,
                    &node.host,
                    node.port,
                    &node.username,
                    &node.gpu_category,
                    node.gpu_count,
                    node.hourly_rate_cents,
                )
                .await
                .map_err(|e| Status::internal(format!("failed to register node: {e}")))?;

            // Get collateral status for this node
            if let Some(ref manager) = self.collateral_manager {
                let (state, status) = manager
                    .get_collateral_status(
                        &req.miner_hotkey,
                        &node.node_id,
                        &node.gpu_category,
                        node.gpu_count,
                    )
                    .await
                    .map_err(|e| Status::internal(format!("collateral status error: {e}")))?;
                worst_collateral_status =
                    Some(select_worst_status(worst_collateral_status, state, status));
            }
        }

        info!(
            miner_hotkey = %req.miner_hotkey,
            miner_id = %miner_id,
            registration_id = %registration_id,
            node_count = req.nodes.len(),
            "Accepted miner registration via RegisterBid"
        );

        // Return validator's SSH public key for miner to deploy
        let validator_ssh_public_key = self.validator_ssh_public_key.clone();

        Ok(Response::new(RegisterBidResponse {
            accepted: true,
            registration_id,
            validator_ssh_public_key,
            health_check_interval_secs: self.auction_config.health_check_interval_secs as u32,
            error_message: String::new(),
            collateral_status: worst_collateral_status.map(status_to_proto),
        }))
    }

    async fn update_bid(
        &self,
        request: Request<UpdateBidRequest>,
    ) -> Result<Response<UpdateBidResponse>, Status> {
        let req = request.into_inner();

        // Validate required fields
        if req.miner_hotkey.trim().is_empty() {
            return Err(Status::invalid_argument("miner_hotkey is required"));
        }
        if req.node_id.trim().is_empty() {
            return Err(Status::invalid_argument("node_id is required"));
        }
        if req.hourly_rate_cents == 0 {
            return Err(Status::invalid_argument(
                "hourly_rate_cents must be greater than 0",
            ));
        }
        if req.signature.is_empty() {
            return Err(Status::invalid_argument("signature is required"));
        }

        // Verify timestamp freshness
        self.ensure_timestamp_freshness(req.timestamp)?;

        // Verify signature
        let message = self.build_update_bid_message(&req);
        self.verify_signature(&req.miner_hotkey, &req.signature, &message)?;

        // Resolve miner ID
        let miner_id = self.resolve_miner_id(&req.miner_hotkey).await?;

        // Update node price
        let updated = self
            .persistence
            .update_node_hourly_rate(&miner_id, &req.node_id, req.hourly_rate_cents)
            .await
            .map_err(|e| Status::internal(format!("failed to update node: {e}")))?;

        if !updated {
            return Err(Status::not_found("node not found"));
        }

        info!(
            miner_hotkey = %req.miner_hotkey,
            node_id = %req.node_id,
            hourly_rate_cents = req.hourly_rate_cents,
            "Updated node price via UpdateBid"
        );

        Ok(Response::new(UpdateBidResponse {
            accepted: true,
            error_message: String::new(),
        }))
    }

    async fn remove_bid(
        &self,
        request: Request<RemoveBidRequest>,
    ) -> Result<Response<RemoveBidResponse>, Status> {
        let req = request.into_inner();

        // Validate required fields
        if req.miner_hotkey.trim().is_empty() {
            return Err(Status::invalid_argument("miner_hotkey is required"));
        }
        if req.signature.is_empty() {
            return Err(Status::invalid_argument("signature is required"));
        }

        // Verify timestamp freshness
        self.ensure_timestamp_freshness(req.timestamp)?;

        // Verify signature
        let message = self.build_remove_bid_message(&req);
        self.verify_signature(&req.miner_hotkey, &req.signature, &message)?;

        // Resolve miner ID
        let miner_id = self.resolve_miner_id(&req.miner_hotkey).await?;

        // Remove nodes
        let removed = self
            .persistence
            .remove_registered_nodes(&miner_id, &req.node_ids)
            .await
            .map_err(|e| Status::internal(format!("failed to remove nodes: {e}")))?;

        info!(
            miner_hotkey = %req.miner_hotkey,
            nodes_removed = removed,
            "Removed nodes via RemoveBid"
        );

        Ok(Response::new(RemoveBidResponse {
            accepted: true,
            nodes_removed: removed,
            error_message: String::new(),
        }))
    }

    async fn health_check(
        &self,
        request: Request<HealthCheckRequest>,
    ) -> Result<Response<HealthCheckResponse>, Status> {
        let req = request.into_inner();

        // Validate required fields
        if req.miner_hotkey.trim().is_empty() {
            return Err(Status::invalid_argument("miner_hotkey is required"));
        }
        if req.signature.is_empty() {
            return Err(Status::invalid_argument("signature is required"));
        }

        // Verify timestamp freshness (use shorter window for health checks)
        self.ensure_timestamp_freshness(req.timestamp)?;

        // Verify signature
        let message = self.build_health_check_message(&req);
        self.verify_signature(&req.miner_hotkey, &req.signature, &message)?;

        // Resolve miner ID
        let miner_id = self.resolve_miner_id(&req.miner_hotkey).await?;

        // Update health check timestamp
        let updated = self
            .persistence
            .update_nodes_health_check(&miner_id, &req.node_ids)
            .await
            .map_err(|e| Status::internal(format!("failed to update health check: {e}")))?;

        Ok(Response::new(HealthCheckResponse {
            accepted: true,
            nodes_active: updated,
            error_message: String::new(),
        }))
    }
}

/// Start the MinerRegistration gRPC server
pub async fn start_registration_server(
    config: GrpcServerConfig,
    persistence: Arc<SimplePersistence>,
    auction_config: AuctionConfig,
    collateral_manager: Option<Arc<CollateralManager>>,
    validator_ssh_public_key: String,
) -> Result<()> {
    let service = RegistrationService::new(
        persistence,
        auction_config,
        collateral_manager,
        validator_ssh_public_key,
    );
    let addr = config
        .listen_address
        .parse()
        .map_err(|e| anyhow::anyhow!("invalid gRPC listen address: {}", e))?;

    let (mut health_reporter, health_service) = health_reporter();
    health_reporter
        .set_serving::<MinerRegistrationServer<RegistrationService>>()
        .await;

    info!(address = %config.listen_address, "Starting miner registration gRPC server");

    Server::builder()
        .add_service(health_service)
        .add_service(MinerRegistrationServer::new(service))
        .serve(addr)
        .await
        .map_err(|e| anyhow::anyhow!("registration gRPC server failed: {}", e))?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    async fn create_test_service() -> RegistrationService {
        let persistence = SimplePersistence::for_testing().await.unwrap();
        RegistrationService::new(
            Arc::new(persistence),
            AuctionConfig::default(),
            None,
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestPublicKey test@validator".to_string(),
        )
    }

    #[tokio::test]
    async fn test_build_register_bid_message() {
        let service = create_test_service().await;

        let req = RegisterBidRequest {
            miner_hotkey: "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string(),
            nodes: vec![],
            timestamp: 1234567890,
            signature: vec![],
        };

        let message = service.build_register_bid_message(&req);
        assert_eq!(
            message,
            "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY|1234567890"
        );
    }

    #[tokio::test]
    async fn test_build_update_bid_message() {
        let service = create_test_service().await;

        let req = UpdateBidRequest {
            miner_hotkey: "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string(),
            node_id: "node-1".to_string(),
            hourly_rate_cents: 250,
            timestamp: 1234567890,
            signature: vec![],
        };

        let message = service.build_update_bid_message(&req);
        assert_eq!(
            message,
            "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY|node-1|250|1234567890"
        );
    }

    #[tokio::test]
    async fn test_build_remove_bid_message() {
        let service = create_test_service().await;

        let req = RemoveBidRequest {
            miner_hotkey: "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string(),
            node_ids: vec!["node-1".to_string(), "node-2".to_string()],
            timestamp: 1234567890,
            signature: vec![],
        };

        let message = service.build_remove_bid_message(&req);
        assert_eq!(
            message,
            "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY|node-1,node-2|1234567890"
        );
    }

    #[tokio::test]
    async fn test_build_health_check_message() {
        let service = create_test_service().await;

        // Test with empty node_ids (all nodes)
        let req = HealthCheckRequest {
            miner_hotkey: "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string(),
            node_ids: vec![],
            timestamp: 1234567890,
            signature: vec![],
        };

        let message = service.build_health_check_message(&req);
        assert_eq!(
            message,
            "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY||1234567890"
        );

        // Test with specific node_ids
        let req = HealthCheckRequest {
            miner_hotkey: "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY".to_string(),
            node_ids: vec!["node-1".to_string(), "node-2".to_string()],
            timestamp: 1234567890,
            signature: vec![],
        };

        let message = service.build_health_check_message(&req);
        assert_eq!(
            message,
            "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY|node-1,node-2|1234567890"
        );
    }
}
