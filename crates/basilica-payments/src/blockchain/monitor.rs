use crate::{
    metrics::PaymentsMetricsSystem,
    storage::{DepositAccountsRepo, ObservedDepositsRepo, OutboxRepo, PgRepos},
};
use anyhow::Result;
use basilica_common::distributed::postgres_lock::{LeaderElection, LockKey};
use bittensor::connect::{BlockchainMonitor, TransferInfo};
use std::collections::HashSet;
use std::error::Error as StdError;
use std::sync::Arc;
use tracing::{error, info};

/// Process transfers and update database
async fn process_transfers(
    repos: &PgRepos,
    transfers: Vec<TransferInfo>,
    known_accounts: &HashSet<String>,
    metrics: Option<&Arc<PaymentsMetricsSystem>>,
) -> Result<()> {
    for transfer in transfers {
        // Check if recipient is a known deposit account
        if !known_accounts.contains(&transfer.to) {
            continue;
        }

        info!(
            "Processing deposit: {} -> {} amount: {} (block: {})",
            transfer.from, transfer.to, transfer.amount, transfer.block_number
        );

        info!("DEPOSIT DETECTED! Transfer to known account {}", transfer.to);

        // Record deposit
        let txid = format!("b{}#e{}#{}", transfer.block_number, transfer.event_index, transfer.to);

        if let Err(e) = async {
            let mut tx = repos.begin().await?;
            repos
                .insert_finalized_tx(
                    &mut tx,
                    transfer.block_number as i64,
                    transfer.event_index as i32,
                    &transfer.to,
                    &transfer.from,
                    &transfer.amount,
                )
                .await?;
            repos.enqueue_tx(&mut tx, &transfer.to, &transfer.amount, &txid).await?;
            tx.commit().await?;
            Ok::<(), anyhow::Error>(())
        }
        .await
        {
            error!("Failed to persist deposit: {}", e);
            continue;
        }

        info!("Recorded deposit: {}", txid);

        // Update metrics
        if let Some(metrics) = metrics {
            let amount_tao = transfer.amount.parse::<f64>().unwrap_or(0.0) / 1e9;
            metrics
                .business_metrics()
                .record_payment_processed(amount_tao, &[("type", "deposit")])
                .await;
        }
    }

    Ok(())
}

/// Monitors blockchain for deposits to payment accounts
pub struct ChainMonitor {
    repos: PgRepos,
    endpoints: Vec<String>,
    metrics: Option<Arc<PaymentsMetricsSystem>>,
}

impl ChainMonitor {
    /// Create a new chain monitor
    pub async fn new(
        repos: PgRepos,
        endpoints: Vec<String>,
        metrics: Option<Arc<PaymentsMetricsSystem>>,
    ) -> Result<Self> {
        Ok(Self {
            repos,
            endpoints,
            metrics,
        })
    }

    /// Run the monitor with leader election
    ///
    /// This uses the common distributed locking to ensure only one monitor
    /// instance is active at a time in a distributed deployment.
    pub async fn run(self) -> Result<()> {
        info!("===== BLOCKCHAIN MONITOR STARTING =====");
        info!("Starting blockchain monitor with leader election");

        let election = LeaderElection::new(self.repos.pool.clone(), LockKey::PAYMENTS_MONITOR)
            .with_retry_interval(3);

        let repos = self.repos;
        let endpoints = self.endpoints.clone();
        let metrics = self.metrics.clone();

        info!("Attempting to acquire leader lock for blockchain monitoring...");

        election
            .run_as_leader(move || {
                let repos = repos.clone();
                let endpoints = endpoints.clone();
                let metrics = metrics.clone();

                async move {
                    info!("Blockchain monitor acquired leadership, initializing...");

                    // Record connection status
                    if let Some(ref metrics) = metrics {
                        metrics
                            .business_metrics()
                            .set_blockchain_connected(true)
                            .await;
                    }

                    // Use first endpoint from the list
                    let endpoint = endpoints.first()
                        .ok_or_else(|| Box::<dyn StdError>::from("No blockchain endpoints configured"))?
                        .clone();

                    info!("Connecting to blockchain at: {}", endpoint);
                    let monitor = BlockchainMonitor::new(&endpoint)
                        .await
                        .map_err(|e| {
                            error!("Failed to connect to blockchain: {}", e);
                            Box::<dyn StdError>::from(e.to_string())
                        })?;

                    info!("Successfully connected to blockchain");

                    // Record connection
                    if let Some(ref m) = metrics {
                        m.business_metrics().set_blockchain_connected(true).await;
                    }

                    // Get initial block number
                    let mut last_block = monitor.get_current_block().await
                        .map_err(|e| Box::<dyn StdError>::from(e.to_string()))?;

                    info!("Starting from block: {}", last_block);

                    // Main monitoring loop
                    let scan_interval = tokio::time::Duration::from_secs(12);
                    let mut ticker = tokio::time::interval(scan_interval);

                    loop {
                        ticker.tick().await;

                        match monitor.get_current_block().await {
                            Ok(current_block) => {
                                if current_block > last_block {
                                    // Get transfers from latest block
                                    match monitor.get_latest_transfers().await {
                                        Ok(transfers) => {
                                            if !transfers.is_empty() {
                                                info!("Found {} transfers in block {}", transfers.len(), current_block);

                                                // Debug: Log all transfers
                                                for t in &transfers {
                                                    info!("  Transfer: {} -> {}", &t.from[0..8], &t.to[0..8]);
                                                }

                                                // Load known accounts
                                                match repos.list_account_hexes().await {
                                                    Ok(accounts) => {
                                                        let known_accounts: HashSet<String> = accounts.into_iter().collect();
                                                        info!("Checking against {} known accounts", known_accounts.len());

                                                        // Process transfers
                                                        if let Err(e) = process_transfers(&repos, transfers, &known_accounts, metrics.as_ref()).await {
                                                            error!("Failed to process transfers: {}", e);
                                                        }
                                                    }
                                                    Err(e) => error!("Failed to load accounts: {}", e),
                                                }
                                            }

                                            // Update metrics
                                            if let Some(ref m) = metrics {
                                                m.business_metrics().set_block_height(current_block as u64).await;
                                            }

                                            last_block = current_block;
                                        }
                                        Err(e) => error!("Failed to get transfers: {}", e),
                                    }
                                }
                            }
                            Err(e) => error!("Failed to get current block: {}", e),
                        }
                    }

                    // Loop never returns, but if it somehow does:
                    #[allow(unreachable_code)]
                    Ok(())
                }
            })
            .await
    }
}
