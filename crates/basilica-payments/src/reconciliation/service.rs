use crate::{
    blockchain::client::BlockchainClient,
    config::ReconciliationConfig,
    error::{PaymentsError, Result},
    metrics::PaymentsMetricsSystem,
    reconciliation::{
        SkipReason, SweepCalculator, SweepDecision, SweepStatus, SweepSummary, WalletManager,
    },
    storage::{DepositAccountsRepo, PgRepos, ReconciliationRepo},
};
use anyhow::Context;
use basilica_common::{
    crypto::Aead,
    distributed::postgres_lock::{LeaderElection, LockKey},
};
use std::sync::Arc;
use tracing::{debug, error, info, warn};

pub struct ReconciliationService {
    repos: PgRepos,
    blockchain: Arc<BlockchainClient>,
    wallet_manager: WalletManager,
    calculator: SweepCalculator,
    config: ReconciliationConfig,
    metrics: Option<Arc<PaymentsMetricsSystem>>,
}

impl ReconciliationService {
    #[allow(clippy::result_large_err)]
    pub fn new(
        repos: PgRepos,
        blockchain: Arc<BlockchainClient>,
        aead: Arc<Aead>,
        config: ReconciliationConfig,
        metrics: Option<Arc<PaymentsMetricsSystem>>,
    ) -> Result<Self> {
        let minimum_threshold = config
            .minimum_threshold_plancks
            .parse()
            .context("Invalid minimum_threshold_plancks")
            .map_err(|e| PaymentsError::Config(e.to_string()))?;

        let target_balance = config
            .target_balance_plancks
            .parse()
            .context("Invalid target_balance_plancks")
            .map_err(|e| PaymentsError::Config(e.to_string()))?;

        let estimated_fee = config
            .estimated_fee_plancks
            .parse()
            .context("Invalid estimated_fee_plancks")
            .map_err(|e| PaymentsError::Config(e.to_string()))?;

        let calculator = SweepCalculator::new(minimum_threshold, target_balance, estimated_fee);
        let wallet_manager = WalletManager::new(aead);

        Ok(Self {
            repos,
            blockchain,
            wallet_manager,
            calculator,
            config,
            metrics,
        })
    }

    pub async fn run(self) -> ! {
        if !self.config.enabled {
            info!("Reconciliation service is disabled");
            loop {
                tokio::time::sleep(tokio::time::Duration::from_secs(3600)).await;
            }
        }

        info!("Starting reconciliation service with leader election");
        info!("Cold wallet: {}", self.config.coldwallet_address_ss58);
        info!("Dry run mode: {}", self.config.dry_run_mode);

        let service = Arc::new(self);
        let election =
            LeaderElection::new(service.repos.pool.clone(), LockKey::RECONCILIATION_SERVICE)
                .with_retry_interval(5);

        election
            .run_as_leader(move || {
                let service_clone = Arc::clone(&service);
                async move {
                    info!("Reconciliation service acquired leadership");
                    service_clone.sweep_loop().await
                }
            })
            .await
    }

    async fn sweep_loop(&self) -> std::result::Result<(), Box<dyn std::error::Error>> {
        let interval = tokio::time::Duration::from_secs(self.config.sweep_interval_seconds);
        let mut ticker = tokio::time::interval(interval);

        loop {
            ticker.tick().await;

            info!("Starting reconciliation sweep cycle");

            match self.sweep_cycle().await {
                Ok(summary) => {
                    info!(
                        "Sweep cycle completed: checked={}, swept={}, failed={}, skipped={}",
                        summary.total_checked,
                        summary.swept_count,
                        summary.failed_count,
                        summary.skipped_count
                    );

                    if let Some(ref metrics) = self.metrics {
                        let amount_tao = summary.total_amount_plancks as f64 / 1e9;
                        metrics
                            .business_metrics()
                            .record_payment_processed(amount_tao, &[("type", "reconciliation")])
                            .await;
                    }
                }
                Err(e) => {
                    error!("Sweep cycle failed: {}", e);
                }
            }
        }
    }

    async fn sweep_cycle(&self) -> Result<SweepSummary> {
        let mut summary = SweepSummary::new();

        let account_hexes = self
            .repos
            .list_account_hexes()
            .await
            .map_err(PaymentsError::Database)?;

        summary.total_checked = account_hexes.len();
        info!("Checking {} deposit accounts", account_hexes.len());

        for account_hex in account_hexes {
            match self.sweep_account(&account_hex).await {
                Ok(Some(amount)) => {
                    summary.swept_count += 1;
                    summary.total_amount_plancks += amount;
                }
                Ok(None) => {
                    summary.skipped_count += 1;
                }
                Err(e) => {
                    let account_preview = account_hex.chars().take(8).collect::<String>();
                    error!("Failed to sweep account {}: {}", account_preview, e);
                    summary.failed_count += 1;
                }
            }
        }

        Ok(summary)
    }

    async fn sweep_account(&self, account_hex: &str) -> Result<Option<u128>> {
        if self
            .repos
            .get_recent_sweep(account_hex, self.config.sweep_interval_seconds as i64)
            .await
            .map_err(PaymentsError::Database)?
        {
            let account_preview = account_hex.chars().take(8).collect::<String>();
            debug!("Skipping {}: recent sweep exists", account_preview);
            return Ok(None);
        }

        let balance = self
            .blockchain
            .get_balance(account_hex)
            .await
            .context("Failed to get balance")
            .map_err(|e| PaymentsError::Blockchain(e.to_string()))?;

        let account_preview = account_hex.chars().take(8).collect::<String>();
        let balance_tao = balance as f64 / 1e9;
        info!(
            "Account {} balance: {} TAO ({} plancks)",
            account_preview, balance_tao, balance
        );

        let decision = self.calculator.calculate(balance);

        match decision {
            SweepDecision::Sweep { amount_plancks } => {
                let account_preview = account_hex.chars().take(8).collect::<String>();
                info!(
                    "Sweeping {} from {}: {} plancks",
                    amount_plancks / 1_000_000_000,
                    account_preview,
                    amount_plancks
                );

                self.execute_sweep(account_hex, balance, amount_plancks)
                    .await?;

                Ok(Some(amount_plancks))
            }
            SweepDecision::Skip { reason } => {
                let account_preview = account_hex.chars().take(8).collect::<String>();
                let balance_tao = balance as f64 / 1e9;
                match reason {
                    SkipReason::BelowThreshold => {
                        info!(
                            "Skipping {}: balance {} TAO below threshold 0.01 TAO",
                            account_preview, balance_tao
                        );
                    }
                    SkipReason::InsufficientForFees => {
                        info!(
                            "Skipping {}: balance {} TAO insufficient after reserve (need > 0.01 TAO)",
                            account_preview, balance_tao
                        );
                    }
                    SkipReason::RecentSweep => {
                        info!("Skipping {}: recent sweep exists", account_preview);
                    }
                }
                Ok(None)
            }
        }
    }

    async fn execute_sweep(
        &self,
        account_hex: &str,
        balance_before: u128,
        sweep_amount: u128,
    ) -> Result<()> {
        let account_data = self
            .repos
            .get_by_account_hex(account_hex)
            .await
            .map_err(PaymentsError::Database)?
            .ok_or_else(|| PaymentsError::Reconciliation("Account not found".into()))?;

        let (hotwallet_ss58, _, _, encrypted_mnemonic) = account_data;

        let mut tx = self.repos.begin().await.map_err(PaymentsError::Database)?;

        let sweep_id = self
            .repos
            .insert_sweep_tx(
                &mut tx,
                account_hex,
                &hotwallet_ss58,
                &self.config.coldwallet_address_ss58,
                &balance_before.to_string(),
                &sweep_amount.to_string(),
                &self.config.estimated_fee_plancks,
                self.config.dry_run_mode,
            )
            .await
            .map_err(PaymentsError::Database)?;

        tx.commit().await.map_err(PaymentsError::Database)?;

        if self.config.dry_run_mode {
            info!(
                "DRY RUN: Would sweep {} plancks from {} to {}",
                sweep_amount,
                &hotwallet_ss58[0..10],
                &self.config.coldwallet_address_ss58[0..10]
            );
            let mut tx = self.repos.begin().await.map_err(PaymentsError::Database)?;
            self.repos
                .update_sweep_status_tx(
                    &mut tx,
                    sweep_id,
                    SweepStatus::Confirmed,
                    Some("DRY_RUN"),
                    None,
                    Some(&(balance_before - sweep_amount).to_string()),
                    None,
                )
                .await
                .map_err(PaymentsError::Database)?;
            tx.commit().await.map_err(PaymentsError::Database)?;
            return Ok(());
        }

        let keypair = self
            .wallet_manager
            .decrypt_and_create_keypair(&encrypted_mnemonic)?;

        let receipt = self
            .blockchain
            .transfer(&keypair, &self.config.coldwallet_address_ss58, sweep_amount)
            .await;

        match receipt {
            Ok(receipt) => {
                info!("Transfer successful: tx_hash={}", receipt.tx_hash);

                let balance_after = self.blockchain.get_balance(account_hex).await.unwrap_or(0);

                let mut tx = self.repos.begin().await.map_err(PaymentsError::Database)?;
                self.repos
                    .update_sweep_status_tx(
                        &mut tx,
                        sweep_id,
                        SweepStatus::Submitted,
                        Some(&receipt.tx_hash),
                        None,
                        Some(&balance_after.to_string()),
                        None,
                    )
                    .await
                    .map_err(PaymentsError::Database)?;
                tx.commit().await.map_err(PaymentsError::Database)?;
            }
            Err(e) => {
                warn!("Transfer failed: {}", e);

                let mut tx = self.repos.begin().await.map_err(PaymentsError::Database)?;
                self.repos
                    .update_sweep_status_tx(
                        &mut tx,
                        sweep_id,
                        SweepStatus::Failed,
                        None,
                        None,
                        None,
                        Some(&e.to_string()),
                    )
                    .await
                    .map_err(PaymentsError::Database)?;
                tx.commit().await.map_err(PaymentsError::Database)?;

                return Err(e);
            }
        }

        Ok(())
    }
}
