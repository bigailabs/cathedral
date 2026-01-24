use anyhow::{Context, Result};
use basilica_common::identity::MinerUid;
use basilica_common::types::GpuCategory;
use basilica_protocol::billing::accumulate_rewards_response::AccumulationType;
use basilica_protocol::billing::billing_service_client::BillingServiceClient;
use basilica_protocol::billing::{
    AccumulateRewardsRequest, GetPendingStatusRequest, MinerDelivery, ProcessThresholdRequest,
    ProcessValidationFailureRequest, UpdateUptimeRequest,
};
use rust_decimal::prelude::{FromPrimitive, ToPrimitive};
use rust_decimal::Decimal;
use std::str::FromStr;
use std::sync::Arc;
use tonic::transport::{Channel, ClientTlsConfig, Endpoint};
use tracing::{debug, warn};

use crate::collateral::manager::CollateralManager;
use crate::collateral::price_oracle::PriceOracle;
use crate::collateral::CollateralState;
use crate::config::{BillingConfig, CliffConfig};
use crate::persistence::gpu_profile_repository::GpuProfileRepository;

const MINUTES_PER_DAY: i32 = 24 * 60;

#[derive(Clone)]
pub struct CliffManager {
    channel: Channel,
    collateral_manager: Option<Arc<CollateralManager>>,
    price_oracle: Arc<PriceOracle>,
    gpu_profile_repo: Arc<GpuProfileRepository>,
    config: CliffConfig,
}

impl CliffManager {
    pub async fn new(
        billing_config: &BillingConfig,
        config: CliffConfig,
        collateral_manager: Option<Arc<CollateralManager>>,
        price_oracle: Arc<PriceOracle>,
        gpu_profile_repo: Arc<GpuProfileRepository>,
    ) -> Result<Self> {
        let mut endpoint = Endpoint::from_shared(billing_config.billing_endpoint.clone())
            .with_context(|| {
                format!(
                    "Invalid billing endpoint: {}",
                    billing_config.billing_endpoint
                )
            })?
            .connect_timeout(std::time::Duration::from_secs(billing_config.timeout_secs))
            .timeout(std::time::Duration::from_secs(billing_config.timeout_secs));

        if billing_config.use_tls {
            let host = billing_config
                .billing_endpoint
                .trim_start_matches("https://")
                .trim_start_matches("http://")
                .split([':', '/'].as_ref())
                .next()
                .ok_or_else(|| {
                    anyhow::anyhow!("Invalid TLS endpoint: {}", billing_config.billing_endpoint)
                })?;
            endpoint = endpoint
                .tls_config(ClientTlsConfig::new().domain_name(host))
                .with_context(|| "Failed to configure TLS for billing endpoint")?;
        }

        let channel = endpoint
            .connect()
            .await
            .with_context(|| "Failed to connect to billing service")?;

        Ok(Self {
            channel,
            collateral_manager,
            price_oracle,
            gpu_profile_repo,
            config,
        })
    }

    pub fn is_enabled(&self) -> bool {
        self.config.enabled
    }

    pub async fn process_delivery(&self, delivery: MinerDelivery) -> Result<Vec<MinerDelivery>> {
        if !self.config.enabled {
            return Ok(vec![Self::decorate_delivery(
                delivery,
                false,
                "immediate",
                0,
                0.0,
            )]);
        }

        if delivery.node_id.is_empty() {
            return Err(anyhow::anyhow!("node_id is required for cliff processing"));
        }

        let gpu_count = self
            .get_gpu_count_for_category(delivery.miner_uid, &delivery.gpu_category)
            .await
            .unwrap_or(1);

        let has_collateral = if self.config.collateral_bypasses_cliff {
            self.has_sufficient_collateral(
                &delivery.miner_hotkey,
                &delivery.node_id,
                &delivery.gpu_category,
                gpu_count,
            )
            .await
            .unwrap_or(false)
        } else {
            false
        };

        if has_collateral {
            return Ok(vec![Self::decorate_delivery(
                delivery,
                true,
                "immediate",
                0,
                0.0,
            )]);
        }

        let mut client = self.client();
        let pending = client
            .get_pending_rewards_status(GetPendingStatusRequest {
                miner_hotkey: delivery.miner_hotkey.clone(),
                node_id: delivery.node_id.clone(),
            })
            .await
            .context("Failed to get pending rewards status")?
            .into_inner();

        let cliff_days_remaining = Self::cliff_days_remaining(
            self.config.duration_days,
            pending.continuous_uptime_minutes,
        );

        let mut outputs = Vec::new();

        if pending.exists
            && !pending.threshold_reached
            && pending.continuous_uptime_minutes
                >= (self.config.duration_days as i32 * MINUTES_PER_DAY)
            && Self::parse_decimal(&pending.pending_alpha).unwrap_or(Decimal::ZERO) > Decimal::ZERO
        {
            let backpay = client
                .process_threshold_reached(ProcessThresholdRequest {
                    miner_hotkey: delivery.miner_hotkey.clone(),
                    node_id: delivery.node_id.clone(),
                })
                .await
                .context("Failed to process cliff backpay")?
                .into_inner();

            let backpay_usd = Self::parse_decimal(&backpay.backpay_usd)
                .unwrap_or(Decimal::ZERO)
                .to_f64()
                .unwrap_or(0.0);

            // TODO: Decide whether backpay should inherit total_hours or remain zero.
            outputs.push(MinerDelivery {
                miner_hotkey: delivery.miner_hotkey.clone(),
                miner_uid: delivery.miner_uid,
                total_hours: 0.0,
                user_revenue_usd: backpay_usd,
                gpu_category: delivery.gpu_category.clone(),
                miner_payment_usd: backpay_usd,
                has_collateral: false,
                payout_type: "backpay".to_string(),
                cliff_days_remaining: 0,
                pending_alpha: 0.0,
                node_id: delivery.node_id.clone(),
            });
        }

        let alpha_price = self.fetch_alpha_price_usd().await?;
        let epoch_earnings_usd = Decimal::from_f64(delivery.miner_payment_usd)
            .ok_or_else(|| anyhow::anyhow!("Invalid miner_payment_usd"))?;

        let accumulate = client
            .accumulate_miner_rewards(AccumulateRewardsRequest {
                miner_hotkey: delivery.miner_hotkey.clone(),
                node_id: delivery.node_id.clone(),
                epoch_earnings_usd: Self::format_decimal(epoch_earnings_usd),
                alpha_price_usd: Self::format_decimal(alpha_price),
            })
            .await
            .context("Failed to accumulate miner rewards")?
            .into_inner();

        let accumulation_type =
            AccumulationType::try_from(accumulate.result).unwrap_or(AccumulationType::Accumulated);
        match accumulation_type {
            AccumulationType::Accumulated => {
                debug!(
                    miner_hotkey = %delivery.miner_hotkey,
                    node_id = %delivery.node_id,
                    pending_alpha = %accumulate.pending_alpha,
                    "Miner rewards held due to cliff"
                );
            }
            AccumulationType::ImmediatePayout => {
                let immediate_usd = Self::parse_decimal(&accumulate.immediate_usd)
                    .unwrap_or(epoch_earnings_usd)
                    .to_f64()
                    .unwrap_or(delivery.miner_payment_usd);
                outputs.push(
                    Self::decorate_delivery(
                        delivery,
                        false,
                        "immediate",
                        cliff_days_remaining,
                        0.0,
                    )
                    .with_miner_payment(immediate_usd),
                );
            }
            AccumulationType::Unspecified => {
                warn!(
                    miner_hotkey = %delivery.miner_hotkey,
                    node_id = %delivery.node_id,
                    "Billing returned unspecified accumulation type"
                );
            }
        }

        Ok(outputs)
    }

    pub async fn update_miner_uptime(
        &self,
        miner_hotkey: &str,
        node_id: &str,
        uptime_minutes: i32,
    ) -> Result<bool> {
        if !self.config.enabled {
            return Ok(false);
        }

        let mut client = self.client();
        let response = client
            .update_miner_uptime(UpdateUptimeRequest {
                miner_hotkey: miner_hotkey.to_string(),
                node_id: node_id.to_string(),
                uptime_minutes,
            })
            .await
            .context("Failed to update miner uptime")?
            .into_inner();

        Ok(response.threshold_reached)
    }

    pub async fn process_validation_failure(
        &self,
        miner_hotkey: &str,
        node_id: &str,
        reason: &str,
        failure_type: Option<&str>,
    ) -> Result<()> {
        if !self.config.enabled || !self.config.forfeit_on_failure {
            return Ok(());
        }

        let mut client = self.client();
        client
            .process_validation_failure(ProcessValidationFailureRequest {
                miner_hotkey: miner_hotkey.to_string(),
                node_id: node_id.to_string(),
                failure_reason: reason.to_string(),
                failure_type: failure_type.unwrap_or_default().to_string(),
            })
            .await
            .context("Failed to process validation failure")?;

        Ok(())
    }

    fn client(&self) -> BillingServiceClient<Channel> {
        BillingServiceClient::new(self.channel.clone())
    }

    async fn has_sufficient_collateral(
        &self,
        miner_hotkey: &str,
        node_id: &str,
        gpu_category: &str,
        gpu_count: u32,
    ) -> Result<bool> {
        let Some(collateral_manager) = &self.collateral_manager else {
            return Ok(false);
        };

        let (state, _) = collateral_manager
            .get_collateral_status(miner_hotkey, node_id, gpu_category, gpu_count)
            .await?;

        Ok(matches!(
            state,
            CollateralState::Sufficient { .. } | CollateralState::Warning { .. }
        ))
    }

    async fn fetch_alpha_price_usd(&self) -> Result<Decimal> {
        let snapshot = self
            .price_oracle
            .get_alpha_usd_price()
            .await
            .context("Failed to fetch Alpha price")?;
        Decimal::from_f64(snapshot.alpha_usd)
            .ok_or_else(|| anyhow::anyhow!("Invalid alpha price: {}", snapshot.alpha_usd))
    }

    async fn get_gpu_count_for_category(&self, miner_uid: u32, gpu_category: &str) -> Result<u32> {
        let assignments = self
            .gpu_profile_repo
            .get_miner_gpu_assignments(MinerUid::new(miner_uid as u16))
            .await?;
        let target = gpu_category.trim().to_uppercase();
        let mut total = 0;
        for (_, (count, name, _)) in assignments {
            let normalized = GpuCategory::from_str(&name)
                .map(|cat| cat.to_string())
                .unwrap_or_else(|_| name.to_uppercase());
            if normalized == target {
                total += count;
            }
        }
        if total == 0 {
            // TODO: Resolve GPU count from miner nodes if assignments are missing.
            total = 1;
        }
        Ok(total)
    }

    fn cliff_days_remaining(duration_days: u32, uptime_minutes: i32) -> i32 {
        let cliff_minutes = duration_days as i32 * MINUTES_PER_DAY;
        let remaining_minutes = (cliff_minutes - uptime_minutes).max(0);
        ((remaining_minutes as f64) / MINUTES_PER_DAY as f64).ceil() as i32
    }

    fn decorate_delivery(
        mut delivery: MinerDelivery,
        has_collateral: bool,
        payout_type: &str,
        cliff_days_remaining: i32,
        pending_alpha: f64,
    ) -> MinerDelivery {
        delivery.has_collateral = has_collateral;
        delivery.payout_type = payout_type.to_string();
        delivery.cliff_days_remaining = cliff_days_remaining;
        delivery.pending_alpha = pending_alpha;
        delivery
    }

    fn format_decimal(value: Decimal) -> String {
        let normalized = value.normalize();
        if normalized.fract().is_zero() {
            normalized.trunc().to_string()
        } else {
            let s = normalized.to_string();
            if s.contains('.') {
                s.trim_end_matches('0').trim_end_matches('.').to_string()
            } else {
                s
            }
        }
    }

    fn parse_decimal(value: &str) -> Result<Decimal> {
        Decimal::from_str(value).map_err(|e| anyhow::anyhow!("Invalid decimal value: {e}"))
    }
}

trait MinerDeliveryExt {
    fn with_miner_payment(self, miner_payment_usd: f64) -> Self;
}

impl MinerDeliveryExt for MinerDelivery {
    fn with_miner_payment(mut self, miner_payment_usd: f64) -> Self {
        self.miner_payment_usd = miner_payment_usd;
        self
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::collateral::evaluator::CollateralEvaluator;
    use crate::collateral::grace_tracker::GracePeriodTracker;
    use crate::collateral::manager::{hotkey_ss58_to_hex, node_id_to_hex, CollateralManager};
    use crate::config::collateral::CollateralConfig;
    use crate::persistence::SimplePersistence;
    use basilica_protocol::billing::billing_service_server::{
        BillingService, BillingServiceServer,
    };
    use basilica_protocol::billing::{
        AccumulateRewardsResponse, ApplyCreditsRequest, ApplyCreditsResponse,
        FinalizeRentalRequest, FinalizeRentalResponse, GetActiveRentalsRequest,
        GetActiveRentalsResponse, GetBalanceRequest, GetBalanceResponse, GetMinerDeliveryRequest,
        GetMinerDeliveryResponse, GetMinerRevenueSummaryRequest, GetMinerRevenueSummaryResponse,
        GetPendingStatusResponse, GetRentalStatusRequest, GetRentalStatusResponse,
        GetUnpaidMinerRevenueSummaryRequest, GetUnpaidMinerRevenueSummaryResponse, IngestResponse,
        MarkMinerRevenuePaidRequest, MarkMinerRevenuePaidResponse, ProcessThresholdResponse,
        ProcessValidationFailureResponse, RefreshMinerRevenueSummaryRequest,
        RefreshMinerRevenueSummaryResponse, TelemetryData, TrackRentalRequest, TrackRentalResponse,
        UpdateRentalStatusRequest, UpdateRentalStatusResponse, UpdateUptimeResponse,
        UsageReportRequest, UsageReportResponse,
    };
    use std::net::SocketAddr;
    use tokio::net::TcpListener;
    use tokio_stream::wrappers::TcpListenerStream;
    use tonic::{Request, Response, Status};
    use uuid::Uuid;
    use wiremock::matchers::path;
    use wiremock::{Mock, MockServer, ResponseTemplate};

    #[derive(Default)]
    struct PendingState {
        pending_alpha: Decimal,
        pending_usd: Decimal,
        epochs_accumulated: i32,
        threshold_reached: bool,
        continuous_uptime_minutes: i32,
    }

    #[derive(Clone, Default)]
    struct MockBillingService {
        pending: Arc<tokio::sync::Mutex<PendingState>>,
    }

    #[tonic::async_trait]
    impl BillingService for MockBillingService {
        async fn apply_credits(
            &self,
            _request: Request<ApplyCreditsRequest>,
        ) -> Result<Response<ApplyCreditsResponse>, Status> {
            Err(Status::unimplemented("apply_credits"))
        }

        async fn get_balance(
            &self,
            _request: Request<GetBalanceRequest>,
        ) -> Result<Response<GetBalanceResponse>, Status> {
            Err(Status::unimplemented("get_balance"))
        }

        async fn track_rental(
            &self,
            _request: Request<TrackRentalRequest>,
        ) -> Result<Response<TrackRentalResponse>, Status> {
            Err(Status::unimplemented("track_rental"))
        }

        async fn update_rental_status(
            &self,
            _request: Request<UpdateRentalStatusRequest>,
        ) -> Result<Response<UpdateRentalStatusResponse>, Status> {
            Err(Status::unimplemented("update_rental_status"))
        }

        async fn get_active_rentals(
            &self,
            _request: Request<GetActiveRentalsRequest>,
        ) -> Result<Response<GetActiveRentalsResponse>, Status> {
            Err(Status::unimplemented("get_active_rentals"))
        }

        async fn finalize_rental(
            &self,
            _request: Request<FinalizeRentalRequest>,
        ) -> Result<Response<FinalizeRentalResponse>, Status> {
            Err(Status::unimplemented("finalize_rental"))
        }

        async fn ingest_telemetry(
            &self,
            _request: Request<tonic::Streaming<TelemetryData>>,
        ) -> Result<Response<IngestResponse>, Status> {
            Err(Status::unimplemented("ingest_telemetry"))
        }

        async fn get_usage_report(
            &self,
            _request: Request<UsageReportRequest>,
        ) -> Result<Response<UsageReportResponse>, Status> {
            Err(Status::unimplemented("get_usage_report"))
        }

        async fn refresh_miner_revenue_summary(
            &self,
            _request: Request<RefreshMinerRevenueSummaryRequest>,
        ) -> Result<Response<RefreshMinerRevenueSummaryResponse>, Status> {
            Err(Status::unimplemented("refresh_miner_revenue_summary"))
        }

        async fn get_miner_revenue_summary(
            &self,
            _request: Request<GetMinerRevenueSummaryRequest>,
        ) -> Result<Response<GetMinerRevenueSummaryResponse>, Status> {
            Err(Status::unimplemented("get_miner_revenue_summary"))
        }

        async fn get_miner_delivery(
            &self,
            _request: Request<GetMinerDeliveryRequest>,
        ) -> Result<Response<GetMinerDeliveryResponse>, Status> {
            Ok(Response::new(GetMinerDeliveryResponse {
                deliveries: Vec::new(),
            }))
        }

        async fn accumulate_miner_rewards(
            &self,
            request: Request<AccumulateRewardsRequest>,
        ) -> Result<Response<AccumulateRewardsResponse>, Status> {
            let req = request.into_inner();
            let epoch_usd = Decimal::from_str(&req.epoch_earnings_usd)
                .map_err(|_| Status::invalid_argument("epoch_earnings_usd"))?;
            let alpha_price = Decimal::from_str(&req.alpha_price_usd)
                .map_err(|_| Status::invalid_argument("alpha_price_usd"))?;
            let epoch_alpha = epoch_usd / alpha_price;

            let mut pending = self.pending.lock().await;
            if pending.threshold_reached {
                return Ok(Response::new(AccumulateRewardsResponse {
                    result: AccumulationType::ImmediatePayout as i32,
                    pending_alpha: "0".to_string(),
                    pending_usd: "0".to_string(),
                    epochs_accumulated: 0,
                    immediate_alpha: epoch_alpha.to_string(),
                    immediate_usd: epoch_usd.to_string(),
                }));
            }

            pending.pending_alpha += epoch_alpha;
            pending.pending_usd += epoch_usd;
            pending.epochs_accumulated += 1;
            Ok(Response::new(AccumulateRewardsResponse {
                result: AccumulationType::Accumulated as i32,
                pending_alpha: pending.pending_alpha.to_string(),
                pending_usd: pending.pending_usd.to_string(),
                epochs_accumulated: pending.epochs_accumulated,
                immediate_alpha: "0".to_string(),
                immediate_usd: "0".to_string(),
            }))
        }

        async fn get_pending_rewards_status(
            &self,
            _request: Request<GetPendingStatusRequest>,
        ) -> Result<Response<GetPendingStatusResponse>, Status> {
            let pending = self.pending.lock().await;
            Ok(Response::new(GetPendingStatusResponse {
                exists: true,
                pending_alpha: pending.pending_alpha.to_string(),
                pending_usd: pending.pending_usd.to_string(),
                epochs_accumulated: pending.epochs_accumulated,
                threshold_reached: pending.threshold_reached,
                continuous_uptime_minutes: pending.continuous_uptime_minutes,
                ramp_start_time: None,
                threshold_reached_at: None,
            }))
        }

        async fn process_threshold_reached(
            &self,
            _request: Request<ProcessThresholdRequest>,
        ) -> Result<Response<ProcessThresholdResponse>, Status> {
            let mut pending = self.pending.lock().await;
            let response = ProcessThresholdResponse {
                backpay_alpha: pending.pending_alpha.to_string(),
                backpay_usd: pending.pending_usd.to_string(),
                epochs_paid: pending.epochs_accumulated,
            };
            pending.pending_alpha = Decimal::ZERO;
            pending.pending_usd = Decimal::ZERO;
            pending.epochs_accumulated = 0;
            pending.threshold_reached = true;
            Ok(Response::new(response))
        }

        async fn update_miner_uptime(
            &self,
            request: Request<UpdateUptimeRequest>,
        ) -> Result<Response<UpdateUptimeResponse>, Status> {
            let req = request.into_inner();
            let mut pending = self.pending.lock().await;
            pending.continuous_uptime_minutes = req.uptime_minutes;
            Ok(Response::new(UpdateUptimeResponse {
                threshold_reached: false,
            }))
        }

        async fn process_validation_failure(
            &self,
            _request: Request<ProcessValidationFailureRequest>,
        ) -> Result<Response<ProcessValidationFailureResponse>, Status> {
            let mut pending = self.pending.lock().await;
            let response = ProcessValidationFailureResponse {
                forfeited_alpha: pending.pending_alpha.to_string(),
                forfeited_usd: pending.pending_usd.to_string(),
                epochs_lost: pending.epochs_accumulated,
            };
            pending.pending_alpha = Decimal::ZERO;
            pending.pending_usd = Decimal::ZERO;
            pending.epochs_accumulated = 0;
            Ok(Response::new(response))
        }

        async fn get_unpaid_miner_revenue_summary(
            &self,
            _request: Request<GetUnpaidMinerRevenueSummaryRequest>,
        ) -> Result<Response<GetUnpaidMinerRevenueSummaryResponse>, Status> {
            Err(Status::unimplemented("get_unpaid_miner_revenue_summary"))
        }

        async fn mark_miner_revenue_paid(
            &self,
            _request: Request<MarkMinerRevenuePaidRequest>,
        ) -> Result<Response<MarkMinerRevenuePaidResponse>, Status> {
            Err(Status::unimplemented("mark_miner_revenue_paid"))
        }

        async fn get_rental_status(
            &self,
            _request: Request<GetRentalStatusRequest>,
        ) -> Result<Response<GetRentalStatusResponse>, Status> {
            Err(Status::unimplemented("get_rental_status"))
        }
    }

    async fn start_mock_server(state: MockBillingService) -> Result<SocketAddr> {
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let addr = listener.local_addr()?;
        let service = BillingServiceServer::new(state);
        tokio::spawn(async move {
            tonic::transport::Server::builder()
                .add_service(service)
                .serve_with_incoming(TcpListenerStream::new(listener))
                .await
                .unwrap();
        });
        Ok(addr)
    }

    async fn build_price_oracle() -> Result<Arc<PriceOracle>> {
        let server = MockServer::start().await;
        Mock::given(path("/price"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "price": 1.0
            })))
            .mount(&server)
            .await;
        Ok(Arc::new(PriceOracle::new(
            server.uri(),
            "/price".to_string(),
            chrono::Duration::minutes(60),
            chrono::Duration::hours(1),
        )))
    }

    fn build_delivery(node_id: &str) -> MinerDelivery {
        MinerDelivery {
            miner_hotkey: "miner_hotkey".to_string(),
            miner_uid: 1,
            total_hours: 1.0,
            user_revenue_usd: 10.0,
            gpu_category: "H100".to_string(),
            miner_payment_usd: 10.0,
            has_collateral: false,
            payout_type: String::new(),
            cliff_days_remaining: 0,
            pending_alpha: 0.0,
            node_id: node_id.to_string(),
        }
    }

    #[tokio::test]
    async fn test_cliff_accumulates_until_threshold() -> Result<()> {
        let state = MockBillingService::default();
        let addr = start_mock_server(state.clone()).await?;

        let persistence = SimplePersistence::for_testing().await?;
        let gpu_repo = Arc::new(GpuProfileRepository::new(persistence.pool().clone()));
        let oracle = build_price_oracle().await?;

        let mut billing_config = BillingConfig::default();
        billing_config.enabled = true;
        billing_config.billing_endpoint = format!("http://{}", addr);

        let mut cliff_config = CliffConfig::default();
        cliff_config.enabled = true;
        let cliff_manager =
            CliffManager::new(&billing_config, cliff_config, None, oracle, gpu_repo).await?;

        let delivery = build_delivery("node-1");
        let outputs = cliff_manager.process_delivery(delivery).await?;
        assert!(outputs.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn test_cliff_backpay_and_immediate() -> Result<()> {
        let state = MockBillingService::default();
        {
            let mut pending = state.pending.lock().await;
            pending.pending_alpha = Decimal::new(100, 0);
            pending.pending_usd = Decimal::new(100, 0);
            pending.epochs_accumulated = 10;
            pending.continuous_uptime_minutes = 20_160;
        }

        let addr = start_mock_server(state.clone()).await?;
        let persistence = SimplePersistence::for_testing().await?;
        let gpu_repo = Arc::new(GpuProfileRepository::new(persistence.pool().clone()));
        let oracle = build_price_oracle().await?;

        let mut billing_config = BillingConfig::default();
        billing_config.enabled = true;
        billing_config.billing_endpoint = format!("http://{}", addr);

        let mut cliff_config = CliffConfig::default();
        cliff_config.enabled = true;
        let cliff_manager =
            CliffManager::new(&billing_config, cliff_config, None, oracle, gpu_repo).await?;

        let delivery = build_delivery("node-2");
        let outputs = cliff_manager.process_delivery(delivery).await?;
        assert_eq!(outputs.len(), 2);
        assert!(outputs.iter().any(|d| d.payout_type == "backpay"));
        assert!(outputs.iter().any(|d| d.payout_type == "immediate"));
        Ok(())
    }

    #[tokio::test]
    async fn test_cliff_collateral_bypass() -> Result<()> {
        let state = MockBillingService::default();
        let addr = start_mock_server(state.clone()).await?;

        let persistence = SimplePersistence::for_testing().await?;
        let gpu_repo = Arc::new(GpuProfileRepository::new(persistence.pool().clone()));
        let oracle = build_price_oracle().await?;

        let mut collateral_config = CollateralConfig::default();
        collateral_config.enabled = true;
        let grace_tracker = Arc::new(GracePeriodTracker::new(
            Arc::new(persistence.clone()),
            collateral_config.grace_period(),
        ));
        let evaluator = Arc::new(CollateralEvaluator::new(
            collateral_config.clone(),
            grace_tracker.clone(),
        ));
        let collateral_manager = Arc::new(CollateralManager::new(
            Arc::new(persistence.clone()),
            oracle.clone(),
            evaluator,
            grace_tracker,
            collateral_config,
            None,
        ));

        let hotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY";
        let node_id = Uuid::new_v4().to_string();
        let hotkey_hex = hotkey_ss58_to_hex(hotkey)?;
        let node_hex = node_id_to_hex(&node_id)?;

        sqlx::query(
            "INSERT INTO collateral_status (hotkey, node_id, miner, collateral, updated_at) VALUES (?, ?, ?, ?, ?)",
        )
        .bind(hotkey_hex)
        .bind(node_hex)
        .bind("0x0000000000000000000000000000000000000000")
        .bind("1000000000000000000000")
        .bind(chrono::Utc::now().to_rfc3339())
        .execute(persistence.pool())
        .await?;

        let mut billing_config = BillingConfig::default();
        billing_config.enabled = true;
        billing_config.billing_endpoint = format!("http://{}", addr);

        let cliff_manager = CliffManager::new(
            &billing_config,
            CliffConfig {
                enabled: true,
                duration_days: 14,
                forfeit_on_failure: true,
                collateral_bypasses_cliff: true,
            },
            Some(collateral_manager),
            oracle,
            gpu_repo,
        )
        .await?;

        let delivery = MinerDelivery {
            miner_hotkey: hotkey.to_string(),
            miner_uid: 1,
            total_hours: 1.0,
            user_revenue_usd: 10.0,
            gpu_category: "H100".to_string(),
            miner_payment_usd: 10.0,
            has_collateral: false,
            payout_type: String::new(),
            cliff_days_remaining: 0,
            pending_alpha: 0.0,
            node_id,
        };

        let outputs = cliff_manager.process_delivery(delivery).await?;
        assert_eq!(outputs.len(), 1);
        assert!(outputs[0].has_collateral);
        assert_eq!(outputs[0].payout_type, "immediate");
        Ok(())
    }
}
