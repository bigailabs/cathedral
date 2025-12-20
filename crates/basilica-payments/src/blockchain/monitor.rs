use crate::{
    config::BlockchainConfig,
    metrics::PaymentsMetricsSystem,
    storage::{DepositAccountsRepo, MonitorStateRepo, ObservedDepositsRepo, OutboxRepo, PgRepos},
};
use anyhow::Result;
use basilica_common::distributed::postgres_lock::{LeaderElection, LockKey};
use bittensor::connect::{BlockchainMonitor, TransferInfo};
use std::collections::HashSet;
use std::error::Error as StdError;
use std::sync::Arc;
use tracing::{error, info, warn};

/// Number of scan ticks between account list reloads (at 12s interval = ~2 minutes)
const ACCOUNT_RELOAD_INTERVAL_TICKS: u32 = 10;

/// Number of consecutive connection failures before attempting reconnect
const CONNECTION_FAILURES_BEFORE_RECONNECT: u32 = 3;

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

        info!(
            "DEPOSIT DETECTED! Transfer to known account {}",
            transfer.to
        );

        // Record deposit
        let txid = format!(
            "b{}#e{}#{}",
            transfer.block_number, transfer.event_index, transfer.to
        );

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
            repos
                .enqueue_tx(&mut tx, &transfer.to, &transfer.amount, &txid)
                .await?;
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

/// Configuration for block scanning resilience
#[derive(Debug, Clone)]
pub struct MonitorConfig {
    /// Maximum consecutive failures before skipping to current block
    pub max_block_retries: u32,
    /// Number of blocks kept by non-archive nodes (for staleness detection)
    pub block_retention_threshold: u32,
    /// Maximum block gap before forcing skip to current
    pub max_block_gap: u32,
    /// Enable automatic reconnection on connection failures
    pub auto_reconnect: bool,
}

impl Default for MonitorConfig {
    fn default() -> Self {
        Self {
            max_block_retries: 5,
            block_retention_threshold: 256,
            max_block_gap: 300,
            auto_reconnect: true,
        }
    }
}

impl From<&BlockchainConfig> for MonitorConfig {
    fn from(config: &BlockchainConfig) -> Self {
        Self {
            max_block_retries: config.max_block_retries,
            block_retention_threshold: config.block_retention_threshold,
            max_block_gap: config.max_block_gap,
            auto_reconnect: config.auto_reconnect,
        }
    }
}

/// Monitors blockchain for deposits to payment accounts
pub struct ChainMonitor {
    repos: PgRepos,
    endpoints: Vec<String>,
    metrics: Option<Arc<PaymentsMetricsSystem>>,
    config: MonitorConfig,
}

impl ChainMonitor {
    /// Create a new chain monitor with default configuration
    pub async fn new(
        repos: PgRepos,
        endpoints: Vec<String>,
        metrics: Option<Arc<PaymentsMetricsSystem>>,
    ) -> Result<Self> {
        Ok(Self {
            repos,
            endpoints,
            metrics,
            config: MonitorConfig::default(),
        })
    }

    /// Create a new chain monitor with custom configuration
    pub async fn with_config(
        repos: PgRepos,
        endpoints: Vec<String>,
        metrics: Option<Arc<PaymentsMetricsSystem>>,
        config: MonitorConfig,
    ) -> Result<Self> {
        Ok(Self {
            repos,
            endpoints,
            metrics,
            config,
        })
    }

    /// Run the monitor with leader election
    ///
    /// This uses the common distributed locking to ensure only one monitor
    /// instance is active at a time in a distributed deployment.
    pub async fn run(self) -> Result<()> {
        info!("===== BLOCKCHAIN MONITOR STARTING =====");
        info!("Starting blockchain monitor with leader election");
        info!(
            "Config: max_retries={}, block_retention={}, max_gap={}, auto_reconnect={}",
            self.config.max_block_retries,
            self.config.block_retention_threshold,
            self.config.max_block_gap,
            self.config.auto_reconnect
        );

        let election = LeaderElection::new(self.repos.pool.clone(), LockKey::PAYMENTS_MONITOR)
            .with_retry_interval(3);

        let repos = self.repos;
        let endpoints = self.endpoints.clone();
        let metrics = self.metrics.clone();
        let config = self.config.clone();

        info!("Attempting to acquire leader lock for blockchain monitoring...");

        election
            .run_as_leader(move || {
                let repos = repos.clone();
                let endpoints = endpoints.clone();
                let metrics = metrics.clone();
                let config = config.clone();

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
                    let endpoint = endpoints
                        .first()
                        .ok_or_else(|| {
                            Box::<dyn StdError>::from("No blockchain endpoints configured")
                        })?
                        .clone();

                    info!("Connecting to blockchain at: {}", endpoint);
                    let monitor = BlockchainMonitor::new(&endpoint).await.map_err(|e| {
                        error!("Failed to connect to blockchain: {}", e);
                        Box::<dyn StdError>::from(e.to_string())
                    })?;

                    info!("Successfully connected to blockchain");

                    // Record connection
                    if let Some(ref m) = metrics {
                        m.business_metrics().set_blockchain_connected(true).await;
                    }

                    // Get initial block number from database or blockchain
                    let current_chain_block = monitor
                        .get_current_block()
                        .await
                        .map_err(|e| Box::<dyn StdError>::from(e.to_string()))?;

                    let last_scanned = repos
                        .get_last_scanned_block("payments_monitor")
                        .await
                        .map_err(|e| Box::<dyn StdError>::from(e.to_string()))?;

                    let mut next_block = match last_scanned {
                        Some(block) if block > 0 => {
                            info!("Resuming from last scanned block: {}", block);
                            // Check if block gap is too large
                            let gap = current_chain_block.saturating_sub(block);
                            if gap > config.max_block_gap {
                                warn!(
                                    "Block gap {} exceeds max_block_gap {}, skipping to current block {}. \
                                     WARNING: Deposits between blocks {} and {} may be missed!",
                                    gap, config.max_block_gap, current_chain_block, block, current_chain_block
                                );
                                current_chain_block
                            } else if gap > config.block_retention_threshold {
                                warn!(
                                    "Block gap {} exceeds retention threshold {}, old blocks may be pruned. \
                                     Will attempt to scan but may need to skip.",
                                    gap, config.block_retention_threshold
                                );
                                block + 1
                            } else {
                                block + 1
                            }
                        }
                        _ => {
                            info!(
                                "No previous scan state, starting from current block: {}",
                                current_chain_block
                            );
                            current_chain_block
                        }
                    };

                    // Load known accounts once at startup
                    let mut known_accounts: HashSet<String> = repos
                        .list_account_hexes()
                        .await
                        .map_err(|e| Box::<dyn StdError>::from(e.to_string()))?
                        .into_iter()
                        .collect();

                    info!(
                        "Monitoring {} deposit accounts for incoming transfers",
                        known_accounts.len()
                    );

                    // Main monitoring loop
                    let scan_interval = tokio::time::Duration::from_secs(12);
                    let mut ticker = tokio::time::interval(scan_interval);
                    let mut account_reload_counter = 0;
                    let mut consecutive_failures: u32 = 0;
                    let mut connection_failures: u32 = 0;

                    loop {
                        ticker.tick().await;

                        // Reload known accounts every 10 ticks (2 minutes)
                        account_reload_counter += 1;
                        if account_reload_counter >= ACCOUNT_RELOAD_INTERVAL_TICKS {
                            match repos.list_account_hexes().await {
                                Ok(accounts) => {
                                    known_accounts = accounts.into_iter().collect();
                                    info!("Reloaded {} deposit accounts", known_accounts.len());
                                }
                                Err(e) => error!("Failed to reload accounts: {}", e),
                            }
                            account_reload_counter = 0;
                        }

                        // Get current blockchain height
                        let current_chain_block = match monitor.get_current_block().await {
                            Ok(b) => {
                                connection_failures = 0;
                                b
                            }
                            Err(e) => {
                                connection_failures += 1;
                                error!(
                                    "Failed to get current block (attempt {}): {}",
                                    connection_failures, e
                                );

                                // Try to reconnect if auto_reconnect is enabled
                                if config.auto_reconnect && connection_failures >= CONNECTION_FAILURES_BEFORE_RECONNECT {
                                    warn!("Multiple connection failures, attempting reconnect...");
                                    if let Err(re) = monitor.reconnect().await {
                                        error!("Reconnection failed: {}", re);
                                    } else {
                                        connection_failures = 0;
                                    }
                                }
                                continue;
                            }
                        };

                        // Check for excessive block gap (could happen after long outage)
                        let gap = current_chain_block.saturating_sub(next_block);
                        if gap > config.max_block_gap {
                            warn!(
                                "Block gap {} exceeds max_block_gap {}, skipping from {} to {}. \
                                 WARNING: Deposits in skipped blocks may be missed!",
                                gap, config.max_block_gap, next_block, current_chain_block
                            );

                            // Update database to reflect the skip
                            if let Err(e) = repos
                                .update_last_scanned_block("payments_monitor", current_chain_block)
                                .await
                            {
                                error!("Failed to persist skip progress: {}", e);
                            }

                            next_block = current_chain_block;
                            consecutive_failures = 0;
                            continue;
                        }

                        // Scan all blocks sequentially from next_block up to current
                        while next_block <= current_chain_block {
                            info!("Scanning block {}", next_block);

                            match monitor.get_transfers_at_block(next_block).await {
                                Ok(transfers) => {
                                    consecutive_failures = 0;

                                    if !transfers.is_empty() {
                                        info!(
                                            "Found {} transfers in block {}",
                                            transfers.len(),
                                            next_block
                                        );

                                        for t in &transfers {
                                            let from_preview = t.from.get(..8).unwrap_or(&t.from);
                                            let to_preview = t.to.get(..8).unwrap_or(&t.to);
                                            info!(
                                                "  Transfer: {} -> {} (amount: {})",
                                                from_preview,
                                                to_preview,
                                                &t.amount
                                            );
                                        }

                                        if let Err(e) = process_transfers(
                                            &repos,
                                            transfers,
                                            &known_accounts,
                                            metrics.as_ref(),
                                        )
                                        .await
                                        {
                                            error!("Failed to process transfers: {}", e);
                                        }
                                    }

                                    // Persist progress after each block
                                    if let Err(e) = repos
                                        .update_last_scanned_block("payments_monitor", next_block)
                                        .await
                                    {
                                        error!("Failed to persist scan progress: {}", e);
                                    }

                                    // Update metrics
                                    if let Some(ref m) = metrics {
                                        m.business_metrics()
                                            .set_block_height(next_block as u64)
                                            .await;
                                    }

                                    next_block += 1;
                                }
                                Err(e) => {
                                    consecutive_failures += 1;
                                    let is_block_not_found = e.to_string().contains("not found");

                                    error!(
                                        "Failed to get transfers for block {} (attempt {}/{}): {}",
                                        next_block, consecutive_failures, config.max_block_retries, e
                                    );

                                    // Check if we should skip this block
                                    let should_skip = consecutive_failures >= config.max_block_retries
                                        || (is_block_not_found && BlockchainMonitor::is_block_likely_pruned(
                                            current_chain_block,
                                            next_block,
                                            config.block_retention_threshold,
                                        ));

                                    if should_skip {
                                        warn!(
                                            "Skipping block {} after {} failures. \
                                             Block may be pruned or unavailable. \
                                             WARNING: Deposits in this block may be missed!",
                                            next_block, consecutive_failures
                                        );

                                        // Skip to next block
                                        if let Err(pe) = repos
                                            .update_last_scanned_block("payments_monitor", next_block)
                                            .await
                                        {
                                            error!("Failed to persist skip progress: {}", pe);
                                        }

                                        next_block += 1;
                                        consecutive_failures = 0;

                                        // Try reconnecting if this looks like a connection issue
                                        if config.auto_reconnect && !is_block_not_found {
                                            warn!("Attempting reconnect after block failures...");
                                            if let Err(re) = monitor.reconnect().await {
                                                error!("Reconnection failed: {}", re);
                                            }
                                        }
                                    }
                                    break;
                                }
                            }

                            // Catch up quickly through old blocks
                            if next_block < current_chain_block {
                                continue;
                            }
                            break;
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
