//! # Basilca Miner
//!
//! Bittensor neuron that manages a fleet of nodes and serves
//! validator requests for GPU rental and computational challenges.

use anyhow::{Context, Result};
use basilica_common::identity::MinerUid;
use clap::Parser;
use std::path::Path;
use std::sync::Arc;
use tokio::signal;
use tracing::{error, info, warn};

mod bittensor_core;
mod cli;
mod config;
mod metrics;
mod node_manager;
mod persistence;
mod request_verification;
mod services;
mod ssh;
mod validator_comms;
mod validator_discovery;

use bittensor_core::ChainRegistration;
use config::MinerConfig;
use node_manager::NodeManager;
use persistence::RegistrationDb;
use ssh::ValidatorAccessService;
use validator_comms::ValidatorCommsServer;

use crate::cli::{Args, Commands};

/// Main miner state
pub struct MinerState {
    pub config: MinerConfig,
    pub miner_uid: MinerUid,
    pub chain_registration: ChainRegistration,
    pub validator_comms: ValidatorCommsServer,
    pub node_manager: Arc<NodeManager>,
    pub registration_db: RegistrationDb,
    pub ssh_access_service: ValidatorAccessService,
    pub metrics: Option<metrics::MinerMetrics>,
    pub validator_discovery: Option<std::sync::Arc<validator_discovery::ValidatorDiscovery>>,
}

impl MinerState {
    /// Initialize miner state
    pub async fn new(config: MinerConfig, enable_metrics: bool) -> Result<Self> {
        info!("Initializing miner...");

        // Initialize metrics system if enabled
        let metrics = if enable_metrics && config.metrics.enabled {
            let miner_metrics = metrics::MinerMetrics::new(config.metrics.clone())?;
            Some(miner_metrics)
        } else {
            None
        };

        // Initialize persistence layer
        let registration_db = RegistrationDb::new(&config.database).await?;

        // Initialize assignment database and run migrations
        let assignment_pool = sqlx::SqlitePool::connect(&config.database.url).await?;
        let assignment_db = persistence::AssignmentDb::new(assignment_pool.clone());
        assignment_db.run_migrations().await?;

        // Initialize node manager with SSH config
        let node_manager = Arc::new(NodeManager::new(config.ssh_session.clone()));

        // Register all configured nodes
        info!(
            "Registering {} configured nodes",
            config.node_management.nodes.len()
        );
        for node_config in &config.node_management.nodes {
            let mut node = node_config.clone();
            // Generate node_id if not provided
            if node.node_id.is_empty() {
                node.node_id = format!("node-{}:{}", node.host, node.port);
            }

            match node_manager.register_node(node).await {
                Ok(_) => info!(
                    "Registered node {} at {}:{}",
                    node_config.node_id, node_config.host, node_config.port
                ),
                Err(e) => error!("Failed to register node {}: {}", node_config.node_id, e),
            }
        }

        // Verify at least one node is registered
        let registered_nodes = node_manager.list_nodes().await?;
        if registered_nodes.is_empty() {
            warn!("No nodes registered - miner will not be able to serve validators");
        } else {
            info!("Successfully registered {} nodes", registered_nodes.len());
        }

        // Initialize SSH services
        let ssh_access_service = ValidatorAccessService::new(node_manager.clone())?;

        // Initialize Bittensor chain registration
        let chain_registration = ChainRegistration::new(config.bittensor.clone()).await?;

        // Initialize validator discovery based on configuration
        let validator_discovery = if config.bittensor.skip_registration
            || !config.validator_assignment.enabled
        {
            info!("Validator discovery disabled (local testing mode)");
            None
        } else {
            let strategy: Box<dyn validator_discovery::AssignmentStrategy> = match config
                .validator_assignment
                .strategy
                .as_str()
            {
                "round_robin" => Box::new(validator_discovery::RoundRobinAssignment),
                "highest_stake" => {
                    let min_stake_rao =
                        (config.validator_assignment.min_stake_threshold * 1_000_000_000.0) as u128;
                    let strategy = validator_discovery::HighestStakeAssignment::new(
                        assignment_pool.clone(),
                        min_stake_rao,
                        config.validator_assignment.validator_hotkey.clone(),
                    );
                    Box::new(strategy)
                }
                _ => {
                    return Err(anyhow::anyhow!(
                                "Unknown assignment strategy '{}'. Valid strategies are: highest_stake, round_robin",
                                config.validator_assignment.strategy
                            ));
                }
            };

            let discovery = validator_discovery::ValidatorDiscovery::new(
                chain_registration.get_bittensor_service(),
                node_manager.clone(),
                strategy,
                config.bittensor.common.netuid,
            );
            Some(std::sync::Arc::new(discovery))
        };

        // Initialize validator communications server
        let validator_comms = ValidatorCommsServer::new(
            config.validator_comms.clone(),
            config.security.clone(),
            node_manager.clone(),
            validator_discovery.clone(),
            Some(chain_registration.get_bittensor_service()),
        )
        .await?;

        // Use a placeholder UID that will be updated after chain registration
        let miner_uid = MinerUid::from(0);

        Ok(Self {
            config,
            miner_uid,
            chain_registration,
            validator_comms,
            node_manager,
            registration_db,
            ssh_access_service,
            metrics,
            validator_discovery,
        })
    }

    /// Run health check on all components
    pub async fn health_check(&self) -> Result<()> {
        info!("Running miner health check...");

        // Check database connection
        self.registration_db.health_check().await?;

        info!("Miner components healthy");
        Ok(())
    }

    /// Start all miner services
    pub async fn start_services(&self) -> Result<()> {
        info!("Starting miner services...");

        // Start metrics server if enabled
        if let Some(ref metrics) = self.metrics {
            metrics.start_server().await?;
            info!("Miner metrics server started");
        }

        // Perform one-time chain registration (Bittensor network presence)
        self.chain_registration.register_startup().await?;

        // Log the discovered UID
        if let Some(uid) = self.chain_registration.get_discovered_uid().await {
            info!("Miner registered with discovered UID: {}", uid);
        } else {
            warn!("No UID discovered - miner may not be registered on chain");
        }

        // Start validator communications server
        let validator_handle = {
            let validator_comms = self.validator_comms.clone();
            tokio::spawn(async move {
                if let Err(e) = validator_comms.start().await {
                    error!("Validator comms server error: {}", e);
                }
            })
        };

        // Start stake monitor service
        let stake_monitor_handle = {
            let config = self.config.clone();
            let pool = sqlx::SqlitePool::connect(&config.database.url)
                .await
                .context("Failed to create pool for stake monitor")?;
            tokio::spawn(async move {
                match services::StakeMonitor::new(&config, pool).await {
                    Ok(monitor) => {
                        info!("Starting stake monitor service");
                        if let Err(e) = monitor.start().await {
                            error!("Stake monitor error: {}", e);
                        }
                    }
                    Err(e) => {
                        error!("Failed to create stake monitor: {}", e);
                    }
                }
            })
        };

        // Start validator discovery service if enabled
        let discovery_handle = if let Some(ref discovery) = self.validator_discovery {
            let discovery = discovery.clone();
            let discovery_interval = tokio::time::Duration::from_secs(600); // 10 minutes
            Some(tokio::spawn(async move {
                info!("Starting validator discovery service");
                loop {
                    if let Err(e) = discovery.run_discovery().await {
                        error!("Validator discovery error: {}", e);
                    }
                    tokio::time::sleep(discovery_interval).await;
                }
            }))
        } else {
            info!("Validator discovery disabled (local testing mode)");
            None
        };

        info!("All miner services started successfully");

        // Wait for shutdown signal
        if let Some(discovery_handle) = discovery_handle {
            tokio::select! {
                _ = signal::ctrl_c() => {
                    info!("Received shutdown signal, stopping miner...");
                }
                _ = validator_handle => {
                    warn!("Validator comms server stopped unexpectedly");
                }
                _ = stake_monitor_handle => {
                    warn!("Stake monitor service stopped unexpectedly");
                }
                _ = discovery_handle => {
                    warn!("Validator discovery service stopped unexpectedly");
                }
            }
        } else {
            tokio::select! {
                _ = signal::ctrl_c() => {
                    info!("Received shutdown signal, stopping miner...");
                }
                _ = validator_handle => {
                    warn!("Validator comms server stopped unexpectedly");
                }
                _ = stake_monitor_handle => {
                    warn!("Stake monitor service stopped unexpectedly");
                }
            }
        }

        Ok(())
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();

    // Generate config file if requested
    if args.gen_config {
        let config = MinerConfig::default();
        let toml_content = toml::to_string_pretty(&config)?;
        std::fs::write(&args.config, toml_content)?;
        println!("Generated configuration file: {}", args.config.display());
        return Ok(());
    }

    // Initialize logging using the unified system
    let binary_name = env!("CARGO_BIN_NAME").replace("-", "_");
    let default_filter = format!("{}=info", binary_name);
    basilica_common::logging::init_logging(&args.verbosity, &binary_name, &default_filter)?;

    // Load configuration
    let config = load_config(&args.config)?;
    info!("Loaded configuration from: {}", args.config.display());

    // Handle CLI commands if provided
    if let Some(command) = args.command {
        return handle_cli_command(command, &config).await;
    }

    // Initialize miner state
    let state = MinerState::new(config, args.metrics).await?;

    // Run initial health check
    if let Err(e) = state.health_check().await {
        error!("Initial health check failed: {}", e);
        return Err(e);
    }

    info!("Starting Basilca Miner (UID: {})", state.miner_uid.as_u16());

    // Start all services
    state.start_services().await?;

    info!("Basilca Miner stopped");
    Ok(())
}

/// Handle CLI commands
async fn handle_cli_command(command: Commands, config: &MinerConfig) -> Result<()> {
    match command {
        Commands::Validator { validator_cmd } => {
            let db = RegistrationDb::new(&config.database).await?;
            cli::handle_validator_command(validator_cmd, db).await
        }
        Commands::Service { service_cmd } => cli::handle_service_command(service_cmd, config).await,
        Commands::Database { database_cmd } => {
            cli::handle_database_command(database_cmd, config).await
        }
        Commands::Config { config_cmd } => cli::handle_config_command(config_cmd, config).await,
        Commands::Status => {
            let db = RegistrationDb::new(&config.database).await?;
            cli::show_miner_status(config, db).await
        }
        Commands::Migrate => {
            let mut db_config = config.database.clone();
            db_config.run_migrations = true;
            let _db = RegistrationDb::new(&db_config).await?;
            // Also run assignment migrations
            let assignment_pool = sqlx::SqlitePool::connect(&config.database.url).await?;
            let assignment_db = persistence::AssignmentDb::new(assignment_pool);
            assignment_db.run_migrations().await?;
            println!("Database migrations completed successfully");
            Ok(())
        }
        Commands::DeployNodes {
            dry_run: _,
            only_machines: _,
            status_only: _,
        } => {
            error!("Deploy nodes command is no longer supported - nodes are managed directly");
            Err(anyhow::anyhow!("This command has been deprecated"))
        }
    }
}

/// Load configuration from file and environment
fn load_config(config_path: &Path) -> Result<MinerConfig> {
    use basilica_common::config::ConfigValidation;

    let path = config_path;
    let config = if path.exists() {
        MinerConfig::load_from_file(path)?
    } else {
        MinerConfig::load()?
    };

    // Validate configuration before proceeding
    config.validate()?;

    // Log any warnings
    let warnings = config.warnings();
    if !warnings.is_empty() {
        warn!("Configuration warnings:");
        for warning in warnings {
            warn!("  - {}", warning);
        }
    }

    Ok(config)
}
