use crate::collateral::evidence::{EvidenceStore, SlashEvidence};
use crate::collateral::grace_tracker::GracePeriodTracker;
use crate::collateral::manager::{hotkey_ss58_to_hex, node_id_to_hex};
use crate::config::collateral::CollateralConfig;
use crate::metrics::ValidatorPrometheusMetrics;
use crate::persistence::SimplePersistence;
use alloy_primitives::U256;
use anyhow::Result;
use chrono::Utc;
use collateral_contract::config::{CollateralNetworkConfig, Network};
use collateral_contract::{slash_collateral, slash_collateral_amount};
use std::fs;
use std::sync::Arc;
use tracing::{info, warn};

#[derive(Clone)]
pub struct SlashExecutor {
    config: CollateralConfig,
    evidence_store: EvidenceStore,
    grace_tracker: Arc<GracePeriodTracker>,
    persistence: Arc<SimplePersistence>,
    metrics: Option<Arc<ValidatorPrometheusMetrics>>,
}

impl SlashExecutor {
    pub fn new(
        config: CollateralConfig,
        evidence_store: EvidenceStore,
        grace_tracker: Arc<GracePeriodTracker>,
        persistence: Arc<SimplePersistence>,
        metrics: Option<Arc<ValidatorPrometheusMetrics>>,
    ) -> Self {
        Self {
            config,
            evidence_store,
            grace_tracker,
            persistence,
            metrics,
        }
    }

    pub async fn execute_slash(
        &self,
        miner_hotkey: &str,
        node_id: &str,
        misbehaviour_type: &str,
        details: &str,
        validator_hotkey: &str,
        rental_id: &str,
    ) -> Result<()> {
        let evidence = SlashEvidence {
            rental_id: rental_id.to_string(),
            misbehaviour_type: misbehaviour_type.to_string(),
            timestamp: Utc::now(),
            details: details.to_string(),
            miner_hotkey: miner_hotkey.to_string(),
            node_id: node_id.to_string(),
            validator_hotkey: validator_hotkey.to_string(),
            shadow_mode: self.config.shadow_mode,
        };

        let (url, json) = self.evidence_store.store(&evidence).await?;
        let checksum = compute_md5_checksum(&json);

        if let Some(metrics) = &self.metrics {
            metrics.record_collateral_slash_triggered(misbehaviour_type);
        }

        // Always mark exclusion immediately after slash trigger.
        if let Err(err) = self
            .grace_tracker
            .force_exclude(miner_hotkey, node_id)
            .await
        {
            warn!("Failed to mark node excluded after slash trigger: {}", err);
        }

        if self.config.shadow_mode {
            match self
                .resolve_partial_slash_amount(miner_hotkey, node_id)
                .await
            {
                Ok(Some(amount)) => {
                    info!(
                        "[SHADOW] Would slash {} wei for node {} (hotkey: {}). Evidence: {}",
                        amount, node_id, miner_hotkey, url
                    );
                }
                _ => {
                    info!(
                        "[SHADOW] Would slash full collateral for node {} (hotkey: {}). Evidence: {}",
                        node_id, miner_hotkey, url
                    );
                }
            }
            if let Some(metrics) = &self.metrics {
                metrics.record_collateral_slash_shadow();
            }
            return Ok(());
        }

        let private_key = self
            .config
            .trustee_private_key_file
            .as_ref()
            .map(|path| fs::read_to_string(path))
            .transpose()?
            .unwrap_or_default()
            .trim()
            .to_string();

        if private_key.is_empty() {
            anyhow::bail!("trustee_private_key_file is required when shadow_mode=false");
        }

        let hotkey_bytes = hotkey_ss58_to_bytes(miner_hotkey)?;
        let node_bytes = node_id_to_bytes(node_id)?;

        let network_config = to_network_config(&self.config)?;
        let partial_amount = self
            .resolve_partial_slash_amount(miner_hotkey, node_id)
            .await
            .unwrap_or_else(|err| {
                warn!("Failed to compute partial slash amount: {}", err);
                None
            });
        if let Some(amount) = partial_amount {
            slash_collateral_amount(
                &private_key,
                hotkey_bytes,
                node_bytes,
                amount,
                &url,
                checksum,
                &network_config,
            )
            .await?;
        } else {
            slash_collateral(
                &private_key,
                hotkey_bytes,
                node_bytes,
                &url,
                checksum,
                &network_config,
            )
            .await?;
        }

        if let Some(metrics) = &self.metrics {
            metrics.record_collateral_slash_executed();
        }

        Ok(())
    }

    fn compute_partial_slash_amount(&self, collateral: U256) -> Option<U256> {
        if self.config.slash_fraction >= 1.0 {
            return None;
        }
        if collateral.is_zero() {
            return None;
        }
        let numerator = (self.config.slash_fraction * 10_000.0).round() as u64;
        if numerator == 0 || numerator >= 10_000 {
            return None;
        }
        let mut amount = collateral * U256::from(numerator) / U256::from(10_000u64);
        if amount.is_zero() {
            amount = U256::from(1u64);
        }
        Some(amount)
    }

    async fn resolve_partial_slash_amount(
        &self,
        miner_hotkey: &str,
        node_id: &str,
    ) -> Result<Option<U256>> {
        if self.config.slash_fraction >= 1.0 {
            return Ok(None);
        }
        let hotkey_hex = hotkey_ss58_to_hex(miner_hotkey)?;
        let node_hex = node_id_to_hex(node_id)?;
        // TODO: Consider querying on-chain collateral if local cache is stale or missing.
        let collateral = self
            .persistence
            .get_collateral_amount(&hotkey_hex, &node_hex)
            .await?;
        let collateral = match collateral {
            Some(amount) => amount,
            None => return Ok(None),
        };
        Ok(self.compute_partial_slash_amount(collateral))
    }
}

fn compute_md5_checksum(contents: &[u8]) -> u128 {
    let digest = md5::compute(contents);
    u128::from_be_bytes(digest.0)
}

fn to_network_config(config: &CollateralConfig) -> Result<CollateralNetworkConfig> {
    let network = match config.network.as_str() {
        "mainnet" => Network::Mainnet,
        "testnet" => Network::Testnet,
        "local" => Network::Local,
        other => {
            anyhow::bail!("Unsupported collateral network: {}", other);
        }
    };
    CollateralNetworkConfig::from_network(&network, config.contract_address.clone())
}

fn hotkey_ss58_to_bytes(hotkey: &str) -> Result<[u8; 32]> {
    let hex = hotkey_ss58_to_hex(hotkey)?;
    let decoded = hex::decode(hex)?;
    let bytes: [u8; 32] = decoded
        .as_slice()
        .try_into()
        .map_err(|_| anyhow::anyhow!("hotkey bytes length mismatch"))?;
    Ok(bytes)
}

fn node_id_to_bytes(node_id: &str) -> Result<[u8; 16]> {
    let hex = node_id_to_hex(node_id)?;
    let decoded = hex::decode(hex)?;
    let bytes: [u8; 16] = decoded
        .as_slice()
        .try_into()
        .map_err(|_| anyhow::anyhow!("node_id bytes length mismatch"))?;
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;
    use uuid::Uuid;

    #[tokio::test]
    async fn test_md5_checksum_length() {
        let checksum = compute_md5_checksum(b"test");
        assert!(checksum > 0);
    }

    #[tokio::test]
    async fn test_shadow_mode_does_not_error() {
        let temp = tempdir().unwrap();
        let config = CollateralConfig {
            shadow_mode: true,
            evidence_storage_path: temp.path().to_path_buf(),
            evidence_base_url: "https://validator.example.com/evidence".to_string(),
            ..CollateralConfig::default()
        };
        let store = EvidenceStore::new(
            config.evidence_base_url.clone(),
            config.evidence_storage_path.clone(),
        );
        let persistence = Arc::new(
            crate::persistence::SimplePersistence::for_testing()
                .await
                .unwrap(),
        );
        let grace_tracker = Arc::new(GracePeriodTracker::new(
            persistence.clone(),
            config.grace_period(),
        ));
        let executor = SlashExecutor::new(config, store, grace_tracker, persistence, None);
        executor
            .execute_slash(
                "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
                &Uuid::new_v4().to_string(),
                "bid_won_deployment_failed",
                "{}",
                "validator-hotkey",
                "rental-1",
            )
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn test_compute_partial_slash_amount() {
        let temp = tempdir().unwrap();
        let config = CollateralConfig {
            slash_fraction: 0.5,
            evidence_storage_path: temp.path().to_path_buf(),
            ..CollateralConfig::default()
        };
        let store = EvidenceStore::new(
            config.evidence_base_url.clone(),
            config.evidence_storage_path.clone(),
        );
        let persistence = Arc::new(
            crate::persistence::SimplePersistence::for_testing()
                .await
                .unwrap(),
        );
        let grace_tracker = Arc::new(GracePeriodTracker::new(
            persistence.clone(),
            config.grace_period(),
        ));
        let executor = SlashExecutor::new(config, store, grace_tracker, persistence, None);
        let amount = executor.compute_partial_slash_amount(U256::from(1000u64));
        assert_eq!(amount, Some(U256::from(500u64)));
    }
}
