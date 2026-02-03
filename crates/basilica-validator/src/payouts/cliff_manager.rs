use anyhow::{Context, Result};
use basilica_common::identity::MinerUid;
use basilica_common::types::GpuCategory;
use basilica_protocol::billing::accumulate_rewards_response::AccumulationType;
use basilica_protocol::billing::MinerDelivery;
use rust_decimal::prelude::{FromPrimitive, ToPrimitive};
use rust_decimal::Decimal;
use std::str::FromStr;
use std::sync::Arc;
use tracing::{debug, warn};

use crate::billing::api_client::{
    AccumulateRewardsRequestBody, PendingRewardsQuery, PendingStatusResponseBody,
    ProcessThresholdRequestBody, ProcessValidationFailureRequestBody, UpdateUptimeRequestBody,
    ValidatorSigner,
};
use crate::billing::BillingApiClient;
use crate::collateral::manager::CollateralManager;
use crate::collateral::CollateralState;
use crate::config::{BillingConfig, CliffConfig};
use crate::persistence::gpu_profile_repository::GpuProfileRepository;
use crate::pricing::TokenPriceClient;

const MINUTES_PER_DAY: i32 = 24 * 60;

#[derive(Clone)]
pub struct CliffManager {
    billing_api: BillingApiClient,
    collateral_manager: Option<Arc<CollateralManager>>,
    price_client: Arc<TokenPriceClient>,
    gpu_profile_repo: Arc<GpuProfileRepository>,
    config: CliffConfig,
    netuid: u16,
}

impl CliffManager {
    pub fn new(
        billing_config: &BillingConfig,
        config: CliffConfig,
        signer: Arc<dyn ValidatorSigner>,
        collateral_manager: Option<Arc<CollateralManager>>,
        price_client: Arc<TokenPriceClient>,
        gpu_profile_repo: Arc<GpuProfileRepository>,
        netuid: u16,
    ) -> Result<Self> {
        Ok(Self {
            billing_api: BillingApiClient::new_with_signer(
                billing_config.api_endpoint.clone(),
                signer,
                billing_config.timeout_secs,
            )?,
            collateral_manager,
            price_client,
            gpu_profile_repo,
            config,
            netuid,
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

        self.ensure_node_id(&delivery)?;

        let gpu_count = self.resolve_gpu_count(&delivery).await;
        if self
            .should_bypass_cliff(&delivery, gpu_count)
            .await
            .unwrap_or(false)
        {
            return Ok(vec![Self::decorate_delivery(
                delivery,
                true,
                "immediate",
                0,
                0.0,
            )]);
        }

        let pending = self.fetch_pending_status(&delivery).await?;
        let cliff_days_remaining = Self::cliff_days_remaining(
            self.config.duration_days,
            pending.continuous_uptime_minutes,
        );

        let mut outputs = Vec::new();
        if let Some(backpay) = self.maybe_process_backpay(&delivery, &pending).await? {
            outputs.push(backpay);
        }

        outputs.extend(
            self.accumulate_rewards(delivery, cliff_days_remaining)
                .await?,
        );

        Ok(outputs)
    }

    fn ensure_node_id(&self, delivery: &MinerDelivery) -> Result<()> {
        if delivery.node_id.is_empty() {
            anyhow::bail!("node_id is required for cliff processing");
        }
        Ok(())
    }

    async fn resolve_gpu_count(&self, delivery: &MinerDelivery) -> u32 {
        self.get_gpu_count_for_category(delivery.miner_uid, &delivery.gpu_category)
            .await
            .unwrap_or(1)
    }

    async fn should_bypass_cliff(&self, delivery: &MinerDelivery, gpu_count: u32) -> Result<bool> {
        if !self.config.collateral_bypasses_cliff {
            return Ok(false);
        }
        self.has_sufficient_collateral(
            &delivery.miner_hotkey,
            &delivery.node_id,
            &delivery.gpu_category,
            gpu_count,
        )
        .await
    }

    async fn fetch_pending_status(
        &self,
        delivery: &MinerDelivery,
    ) -> Result<PendingStatusResponseBody> {
        self.billing_api
            .get_pending_rewards_status(PendingRewardsQuery {
                miner_hotkey: delivery.miner_hotkey.clone(),
                node_id: delivery.node_id.clone(),
            })
            .await
            .context("Failed to get pending rewards status")
    }

    async fn maybe_process_backpay(
        &self,
        delivery: &MinerDelivery,
        pending: &PendingStatusResponseBody,
    ) -> Result<Option<MinerDelivery>> {
        if !pending.exists
            || pending.threshold_reached
            || pending.continuous_uptime_minutes
                < (self.config.duration_days as i32 * MINUTES_PER_DAY)
        {
            return Ok(None);
        }

        let pending_alpha = Self::parse_decimal(&pending.pending_alpha).unwrap_or(Decimal::ZERO);
        if pending_alpha <= Decimal::ZERO {
            return Ok(None);
        }

        let backpay = self
            .billing_api
            .process_threshold_reached(ProcessThresholdRequestBody {
                miner_hotkey: delivery.miner_hotkey.clone(),
                node_id: delivery.node_id.clone(),
            })
            .await
            .context("Failed to process cliff backpay")?;

        let backpay_usd = Self::parse_decimal(&backpay.backpay_usd)
            .unwrap_or(Decimal::ZERO)
            .to_f64()
            .unwrap_or(0.0);

        Ok(Some(MinerDelivery {
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
        }))
    }

    async fn accumulate_rewards(
        &self,
        delivery: MinerDelivery,
        cliff_days_remaining: i32,
    ) -> Result<Vec<MinerDelivery>> {
        let alpha_price = self.fetch_alpha_price_usd().await?;
        let epoch_earnings_usd = Decimal::from_f64(delivery.miner_payment_usd)
            .ok_or_else(|| anyhow::anyhow!("Invalid miner_payment_usd"))?;

        let accumulate = self
            .billing_api
            .accumulate_miner_rewards(AccumulateRewardsRequestBody {
                miner_hotkey: delivery.miner_hotkey.clone(),
                node_id: delivery.node_id.clone(),
                epoch_earnings_usd: Self::format_decimal(epoch_earnings_usd),
                alpha_price_usd: Self::format_decimal(alpha_price),
            })
            .await
            .context("Failed to accumulate miner rewards")?;

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
                Ok(Vec::new())
            }
            AccumulationType::ImmediatePayout => {
                let immediate_usd = Self::parse_decimal(&accumulate.immediate_usd)
                    .unwrap_or(epoch_earnings_usd)
                    .to_f64()
                    .unwrap_or(delivery.miner_payment_usd);
                Ok(vec![Self::decorate_delivery(
                    delivery,
                    false,
                    "immediate",
                    cliff_days_remaining,
                    0.0,
                )
                .with_miner_payment(immediate_usd)])
            }
            AccumulationType::Unspecified => {
                warn!(
                    miner_hotkey = %delivery.miner_hotkey,
                    node_id = %delivery.node_id,
                    "Billing returned unspecified accumulation type"
                );
                Ok(Vec::new())
            }
        }
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

        let response = self
            .billing_api
            .update_miner_uptime(UpdateUptimeRequestBody {
                miner_hotkey: miner_hotkey.to_string(),
                node_id: node_id.to_string(),
                uptime_minutes,
            })
            .await
            .context("Failed to update miner uptime")?;

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

        self.billing_api
            .process_validation_failure(ProcessValidationFailureRequestBody {
                miner_hotkey: miner_hotkey.to_string(),
                node_id: node_id.to_string(),
                failure_reason: reason.to_string(),
                failure_type: failure_type.map(|v| v.to_string()),
            })
            .await
            .context("Failed to process validation failure")?;

        Ok(())
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
        self.price_client
            .get_alpha_price_usd(self.netuid)
            .await
            .context("Failed to fetch Alpha price")
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
    use crate::billing::api_client::{
        AccumulateRewardsRequestBody, PendingRewardsQuery, ProcessThresholdRequestBody,
        ProcessValidationFailureRequestBody, UpdateUptimeRequestBody,
    };
    use crate::collateral::evaluator::CollateralEvaluator;
    use crate::collateral::grace_tracker::GracePeriodTracker;
    use crate::collateral::manager::{hotkey_ss58_to_hex, node_id_to_hex, CollateralManager};
    use crate::config::collateral::CollateralConfig;
    use crate::persistence::SimplePersistence;
    use crate::pricing::token_prices::{TokenPriceFetcher, TokenPriceSnapshot};
    use crate::pricing::TokenPriceClient;
    use axum::extract::{Query, State};
    use axum::routing::{get, post};
    use axum::{Json, Router};
    use std::net::SocketAddr;
    use std::time::Duration as StdDuration;
    use tokio::net::TcpListener;
    use uuid::Uuid;

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

    #[derive(Clone, Default)]
    struct TestSigner;

    impl ValidatorSigner for TestSigner {
        fn hotkey(&self) -> String {
            "validator_hotkey".to_string()
        }

        fn sign(&self, _message: &[u8]) -> Result<String> {
            Ok("test-signature".to_string())
        }
    }

    struct TestPriceFetcher;

    #[async_trait::async_trait]
    impl TokenPriceFetcher for TestPriceFetcher {
        async fn fetch(
            &self,
            _api_endpoint: &str,
            _netuid: u16,
            _signer: &dyn ValidatorSigner,
        ) -> Result<TokenPriceSnapshot> {
            Ok(TokenPriceSnapshot {
                tao_price_usd: Decimal::ONE,
                alpha_price_usd: Decimal::ONE,
                alpha_price_tao: Decimal::ONE,
                tao_reserve: Decimal::ONE,
                alpha_reserve: Decimal::ONE,
                fetched_at: "2024-01-01T00:00:00Z".to_string(),
            })
        }
    }

    async fn handle_pending_status(
        State(state): State<MockBillingService>,
        Query(_query): Query<PendingRewardsQuery>,
    ) -> Json<crate::billing::api_client::PendingStatusResponseBody> {
        let pending = state.pending.lock().await;
        Json(crate::billing::api_client::PendingStatusResponseBody {
            exists: true,
            pending_alpha: pending.pending_alpha.to_string(),
            pending_usd: pending.pending_usd.to_string(),
            epochs_accumulated: pending.epochs_accumulated,
            threshold_reached: pending.threshold_reached,
            continuous_uptime_minutes: pending.continuous_uptime_minutes,
            ramp_start_time: None,
            threshold_reached_at: None,
        })
    }

    async fn handle_accumulate(
        State(state): State<MockBillingService>,
        Json(body): Json<AccumulateRewardsRequestBody>,
    ) -> Json<crate::billing::api_client::AccumulateRewardsResponseBody> {
        let epoch_usd = Decimal::from_str(&body.epoch_earnings_usd).unwrap_or(Decimal::ZERO);
        let alpha_price = Decimal::from_str(&body.alpha_price_usd).unwrap_or(Decimal::ONE);
        let epoch_alpha = epoch_usd / alpha_price;

        let mut pending = state.pending.lock().await;
        if pending.threshold_reached {
            return Json(crate::billing::api_client::AccumulateRewardsResponseBody {
                result: AccumulationType::ImmediatePayout as i32,
                pending_alpha: "0".to_string(),
                pending_usd: "0".to_string(),
                epochs_accumulated: 0,
                immediate_alpha: epoch_alpha.to_string(),
                immediate_usd: epoch_usd.to_string(),
            });
        }

        pending.pending_alpha += epoch_alpha;
        pending.pending_usd += epoch_usd;
        pending.epochs_accumulated += 1;
        Json(crate::billing::api_client::AccumulateRewardsResponseBody {
            result: AccumulationType::Accumulated as i32,
            pending_alpha: pending.pending_alpha.to_string(),
            pending_usd: pending.pending_usd.to_string(),
            epochs_accumulated: pending.epochs_accumulated,
            immediate_alpha: "0".to_string(),
            immediate_usd: "0".to_string(),
        })
    }

    async fn handle_threshold(
        State(state): State<MockBillingService>,
        Json(_body): Json<ProcessThresholdRequestBody>,
    ) -> Json<crate::billing::api_client::ProcessThresholdResponseBody> {
        let mut pending = state.pending.lock().await;
        let response = crate::billing::api_client::ProcessThresholdResponseBody {
            backpay_alpha: pending.pending_alpha.to_string(),
            backpay_usd: pending.pending_usd.to_string(),
            epochs_paid: pending.epochs_accumulated,
        };
        pending.pending_alpha = Decimal::ZERO;
        pending.pending_usd = Decimal::ZERO;
        pending.epochs_accumulated = 0;
        pending.threshold_reached = true;
        Json(response)
    }

    async fn handle_uptime(
        State(state): State<MockBillingService>,
        Json(body): Json<UpdateUptimeRequestBody>,
    ) -> Json<crate::billing::api_client::UpdateUptimeResponseBody> {
        let mut pending = state.pending.lock().await;
        pending.continuous_uptime_minutes = body.uptime_minutes;
        Json(crate::billing::api_client::UpdateUptimeResponseBody {
            threshold_reached: false,
        })
    }

    async fn handle_failure(
        State(state): State<MockBillingService>,
        Json(_body): Json<ProcessValidationFailureRequestBody>,
    ) -> Json<crate::billing::api_client::ProcessValidationFailureResponseBody> {
        let mut pending = state.pending.lock().await;
        let response = crate::billing::api_client::ProcessValidationFailureResponseBody {
            forfeited_alpha: pending.pending_alpha.to_string(),
            forfeited_usd: pending.pending_usd.to_string(),
            epochs_lost: pending.epochs_accumulated,
        };
        pending.pending_alpha = Decimal::ZERO;
        pending.pending_usd = Decimal::ZERO;
        pending.epochs_accumulated = 0;
        Json(response)
    }

    async fn start_mock_server(state: MockBillingService) -> Result<SocketAddr> {
        let app = Router::new()
            .route(
                "/v1/weights/pending-rewards/status",
                get(handle_pending_status),
            )
            .route(
                "/v1/weights/pending-rewards/accumulate",
                post(handle_accumulate),
            )
            .route(
                "/v1/weights/pending-rewards/threshold",
                post(handle_threshold),
            )
            .route("/v1/weights/pending-rewards/uptime", post(handle_uptime))
            .route("/v1/weights/pending-rewards/failure", post(handle_failure))
            .with_state(state);

        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let addr = listener.local_addr()?;
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        Ok(addr)
    }

    async fn build_price_client() -> Result<Arc<TokenPriceClient>> {
        let signer: Arc<dyn ValidatorSigner> = Arc::new(TestSigner);
        Ok(Arc::new(TokenPriceClient::new_with_fetcher(
            "http://localhost".to_string(),
            StdDuration::from_secs(60),
            signer,
            Arc::new(TestPriceFetcher),
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
        let price_client = build_price_client().await?;

        let billing_config = BillingConfig {
            enabled: true,
            api_endpoint: format!("http://{}", addr),
            ..Default::default()
        };

        let cliff_config = CliffConfig {
            enabled: true,
            ..Default::default()
        };
        let signer = Arc::new(TestSigner);
        let cliff_manager = CliffManager::new(
            &billing_config,
            cliff_config,
            signer,
            None,
            price_client,
            gpu_repo,
            1,
        )?;

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
        let price_client = build_price_client().await?;

        let billing_config = BillingConfig {
            enabled: true,
            api_endpoint: format!("http://{}", addr),
            ..Default::default()
        };

        let cliff_config = CliffConfig {
            enabled: true,
            ..Default::default()
        };
        let signer = Arc::new(TestSigner);
        let cliff_manager = CliffManager::new(
            &billing_config,
            cliff_config,
            signer,
            None,
            price_client,
            gpu_repo,
            1,
        )?;

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
        let price_client = build_price_client().await?;

        let collateral_config = CollateralConfig {
            enabled: true,
            ..Default::default()
        };
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
            price_client.clone(),
            evaluator,
            grace_tracker,
            collateral_config,
            1,
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

        let billing_config = BillingConfig {
            enabled: true,
            api_endpoint: format!("http://{}", addr),
            ..Default::default()
        };

        let signer = Arc::new(TestSigner);
        let cliff_manager = CliffManager::new(
            &billing_config,
            CliffConfig {
                enabled: true,
                duration_days: 14,
                forfeit_on_failure: true,
                collateral_bypasses_cliff: true,
            },
            signer,
            Some(collateral_manager),
            price_client,
            gpu_repo,
            1,
        )?;

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
