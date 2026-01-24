use crate::collateral::evaluator::{CollateralEvaluator, CollateralState, CollateralStatus};
use crate::collateral::grace_tracker::GracePeriodTracker;
use crate::collateral::price_oracle::PriceOracle;
use crate::config::collateral::CollateralConfig;
use crate::metrics::ValidatorPrometheusMetrics;
use crate::persistence::SimplePersistence;
use anyhow::Result;
use basilica_common::identity::Hotkey;
use chrono::Utc;
use hex::encode;
use std::sync::Arc;
use tracing::warn;
use uuid::Uuid;

const ALPHA_DECIMALS: f64 = 1e18_f64;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CollateralPreference {
    Preferred,
    Fallback,
    Excluded,
}

#[derive(Clone)]
pub struct CollateralManager {
    persistence: Arc<SimplePersistence>,
    price_oracle: Arc<PriceOracle>,
    evaluator: Arc<CollateralEvaluator>,
    grace_tracker: Arc<GracePeriodTracker>,
    config: CollateralConfig,
    metrics: Option<Arc<ValidatorPrometheusMetrics>>,
}

impl CollateralManager {
    pub fn new(
        persistence: Arc<SimplePersistence>,
        price_oracle: Arc<PriceOracle>,
        evaluator: Arc<CollateralEvaluator>,
        grace_tracker: Arc<GracePeriodTracker>,
        config: CollateralConfig,
        metrics: Option<Arc<ValidatorPrometheusMetrics>>,
    ) -> Self {
        Self {
            persistence,
            price_oracle,
            evaluator,
            grace_tracker,
            config,
            metrics,
        }
    }

    pub async fn get_collateral_status(
        &self,
        hotkey: &str,
        node_id: &str,
        gpu_category: &str,
        gpu_count: u32,
    ) -> Result<(CollateralState, CollateralStatus)> {
        if !self.config.enabled {
            return Ok((
                CollateralState::Unknown {
                    reason: "collateral disabled".to_string(),
                },
                CollateralStatus {
                    current_alpha: 0.0,
                    current_usd_value: 0.0,
                    minimum_usd_required: 0.0,
                    status: "unknown".to_string(),
                    grace_period_remaining: None,
                    action_required: None,
                    alpha_usd_price: None,
                    price_stale: true,
                },
            ));
        }

        let price_snapshot = match self.price_oracle.get_alpha_usd_price().await {
            Ok(snapshot) => Some(snapshot),
            Err(err) => {
                warn!("Alpha price unavailable: {}", err);
                None
            }
        };

        if let Some(metrics) = &self.metrics {
            if let Some(snapshot) = &price_snapshot {
                metrics.record_collateral_price(snapshot.alpha_usd, snapshot.is_stale);
                let staleness_seconds =
                    (Utc::now() - snapshot.fetched_at).num_seconds().max(0) as f64;
                metrics.record_collateral_price_staleness_seconds(staleness_seconds);
            }
        }

        let collateral_alpha = self
            .get_collateral_alpha(hotkey, node_id)
            .await
            .unwrap_or(0.0);

        let (state, status) = self
            .evaluator
            .evaluate(
                hotkey,
                node_id,
                gpu_category,
                gpu_count,
                collateral_alpha,
                price_snapshot,
            )
            .await?;
        if let Some(metrics) = &self.metrics {
            metrics.record_collateral_node_status(hotkey, node_id, gpu_category, &status.status);
        }
        Ok((state, status))
    }

    pub async fn get_preference(
        &self,
        hotkey: &str,
        node_id: &str,
        gpu_category: &str,
        gpu_count: u32,
    ) -> CollateralPreference {
        match self
            .get_collateral_status(hotkey, node_id, gpu_category, gpu_count)
            .await
        {
            Ok((state, _)) => match state {
                CollateralState::Sufficient { .. } | CollateralState::Warning { .. } => {
                    CollateralPreference::Preferred
                }
                CollateralState::Undercollateralized { .. } | CollateralState::Unknown { .. } => {
                    CollateralPreference::Fallback
                }
                CollateralState::Excluded { .. } => CollateralPreference::Excluded,
            },
            Err(_) => CollateralPreference::Fallback,
        }
    }

    pub async fn refresh_price_cache(&self) {
        if !self.config.enabled {
            return;
        }
        match self.price_oracle.get_alpha_usd_price().await {
            Ok(snapshot) => {
                if let Some(metrics) = self.metrics.as_ref() {
                    metrics.record_collateral_price(snapshot.alpha_usd, snapshot.is_stale);
                    let staleness_seconds =
                        (Utc::now() - snapshot.fetched_at).num_seconds().max(0) as f64;
                    metrics.record_collateral_price_staleness_seconds(staleness_seconds);
                }
            }
            Err(err) => {
                warn!("Failed to refresh Alpha/USD price: {}", err);
            }
        }
    }

    pub async fn is_eligible_for_bids(
        &self,
        hotkey: &str,
        node_id: &str,
        gpu_category: &str,
        gpu_count: u32,
    ) -> bool {
        match self
            .get_collateral_status(hotkey, node_id, gpu_category, gpu_count)
            .await
        {
            Ok((state, _)) => !matches!(state, CollateralState::Excluded { .. }),
            Err(_) => true,
        }
    }

    pub async fn force_exclude(&self, hotkey: &str, node_id: &str) -> Result<()> {
        self.grace_tracker.force_exclude(hotkey, node_id).await
    }

    pub async fn get_collateral_alpha(&self, hotkey: &str, node_id: &str) -> Result<f64> {
        let hotkey_hex = match hotkey_ss58_to_hex(hotkey) {
            Ok(val) => val,
            Err(err) => {
                warn!("Failed to convert hotkey to hex: {}", err);
                return Ok(0.0);
            }
        };
        let node_hex = match node_id_to_hex(node_id) {
            Ok(val) => val,
            Err(err) => {
                warn!("Failed to convert node_id to hex: {}", err);
                return Ok(0.0);
            }
        };

        let amount = self
            .persistence
            .get_alpha_collateral_amount(&hotkey_hex, &node_hex)
            .await?;
        let amount = amount.unwrap_or_default();
        Ok(u256_to_alpha(amount))
    }
}

pub fn hotkey_ss58_to_hex(hotkey: &str) -> Result<String> {
    let hotkey = Hotkey::new(hotkey.to_string())
        .map_err(|e| anyhow::anyhow!("invalid hotkey: {e}"))?;
    let account_id = hotkey
        .to_account_id()
        .map_err(|e| anyhow::anyhow!("hotkey conversion failed: {e}"))?;
    let account_bytes: &[u8] = account_id.as_ref();
    Ok(encode(account_bytes))
}

pub fn node_id_to_hex(node_id: &str) -> Result<String> {
    let uuid = Uuid::parse_str(node_id)?;
    Ok(encode(uuid.as_bytes()))
}

fn u256_to_alpha(amount: alloy_primitives::U256) -> f64 {
    // TODO: Switch to fixed-point decimal to avoid precision loss for very large values.
    let amount_f64 = amount.to_string().parse::<f64>().unwrap_or(0.0);
    amount_f64 / ALPHA_DECIMALS
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::collateral::price_oracle::PriceOracle;
    use crate::config::collateral::CollateralConfig;
    use crate::persistence::SimplePersistence;
    use chrono::Duration;

    #[tokio::test]
    async fn test_node_id_to_hex() {
        let uuid = Uuid::new_v4();
        let hex = node_id_to_hex(&uuid.to_string()).unwrap();
        assert_eq!(hex.len(), 32);
    }

    #[tokio::test]
    async fn test_get_collateral_alpha_missing_returns_zero() {
        let persistence = Arc::new(SimplePersistence::for_testing().await.unwrap());
        let config = CollateralConfig::default();
        let grace_tracker = Arc::new(GracePeriodTracker::new(
            persistence.clone(),
            Duration::hours(24),
        ));
        let evaluator = Arc::new(CollateralEvaluator::new(config.clone(), grace_tracker.clone()));
        let price_oracle = Arc::new(PriceOracle::new(
            config.taostats_base_url.clone(),
            config.alpha_price_path.clone(),
            config.price_refresh_interval(),
            config.price_stale_after(),
        ));
        let manager = CollateralManager::new(
            persistence.clone(),
            price_oracle,
            evaluator,
            grace_tracker,
            config,
            None,
        );
        let alpha = manager
            .get_collateral_alpha(
                "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
                &Uuid::new_v4().to_string(),
            )
            .await
            .unwrap();
        assert_eq!(alpha, 0.0);
    }
}

