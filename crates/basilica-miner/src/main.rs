//! # Cathedral Miner
//!
//! Bittensor neuron that manages a fleet of nodes and serves
//! validator requests for GPU rental and computational challenges.

use anyhow::Result;
use basilica_common::identity::MinerUid;
use clap::Parser;
use std::path::Path;
use std::sync::Arc;
use tokio::signal;
use tokio::sync::watch;
use tracing::{error, info, warn};

mod bidding;
mod bittensor_core;
mod cli;
mod config;
mod metrics;
mod node_manager;
mod persistence;
mod registration_client;
mod validator_discovery;

use bidding::BidManager;
use bittensor_core::ChainRegistration;
use config::MinerConfig;
use node_manager::NodeManager;
use persistence::RegistrationDb;
use registration_client::RegistrationClient;

use crate::cli::{Args, Commands};

/// Main miner state
pub struct MinerState {
    pub config: MinerConfig,
    pub miner_uid: MinerUid,
    pub chain_registration: ChainRegistration,
    pub registration_client: Arc<RegistrationClient>,
    pub registration_db: RegistrationDb,
    pub metrics: Option<metrics::MinerMetrics>,
    pub validator_discovery: validator_discovery::ValidatorDiscovery,
    pub bid_manager: Arc<BidManager>,
    pub node_manager: Arc<NodeManager>,
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

        // Initialize node manager with SSH config
        let node_manager = Arc::new(NodeManager::new(config.ssh_session.clone()));

        // Register all configured nodes (keyed by host)
        info!(
            "Registering {} configured nodes",
            config.node_management.nodes.len()
        );
        for node_config in &config.node_management.nodes {
            match node_manager.register_node(node_config.clone()).await {
                Ok(_) => info!(
                    "Registered node at {}@{}:{}",
                    node_config.username, node_config.host, node_config.port
                ),
                Err(e) => error!(
                    "Failed to register node at {}@{}:{}: {}",
                    node_config.username, node_config.host, node_config.port, e
                ),
            }
        }

        // Verify at least one node is registered
        let registered_nodes = node_manager.list_nodes().await?;
        if registered_nodes.is_empty() {
            warn!("No nodes registered - miner will not be able to serve validators");
        } else {
            info!("Successfully registered {} nodes", registered_nodes.len());
        }

        // Initialize Bittensor chain registration
        let chain_registration = ChainRegistration::new(config.bittensor.clone()).await?;

        // Initialize validator discovery
        let strategy: Box<dyn validator_discovery::AssignmentStrategy> = match config
            .validator_assignment
            .strategy
            .as_str()
        {
            "highest_stake" => Box::new(validator_discovery::HighestStakeAssignment),
            "fixed_assignment" => {
                let validator_hotkey = config
                    .validator_assignment
                    .validator_hotkey
                    .clone()
                    .expect("validator_hotkey is required for fixed_assignment strategy");
                let has_override = config.validator_assignment.grpc_endpoint_override.is_some();
                Box::new(
                    validator_discovery::FixedAssignment::new(validator_hotkey)
                        .allow_offline(has_override),
                )
            }
            _ => {
                return Err(anyhow::anyhow!(
                    "Unknown assignment strategy '{}'. Valid strategies are: highest_stake, fixed_assignment",
                    config.validator_assignment.strategy
                ));
            }
        };

        let validator_discovery = validator_discovery::ValidatorDiscovery::new(
            chain_registration.get_bittensor_service(),
            node_manager.clone(),
            strategy,
            config.bittensor.common.netuid,
            config.bid_grpc_port,
        );

        // Initialize registration client for miner→validator communication
        let bittensor_service = chain_registration.get_bittensor_service();
        let registration_client = Arc::new(RegistrationClient::new(
            std::time::Duration::from_secs(30),
            node_manager.clone(),
            bittensor_service,
        ));

        // Initialize bid manager (owns registration lifecycle)
        let bid_manager = Arc::new(BidManager::new(
            config.bidding.clone(),
            node_manager.clone(),
            registration_client.clone(),
        ));

        // Use a placeholder UID that will be updated after chain registration
        let miner_uid = MinerUid::from(0);

        Ok(Self {
            config,
            miner_uid,
            chain_registration,
            registration_client,
            registration_db,
            metrics,
            validator_discovery,
            bid_manager,
            node_manager,
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
        match self.chain_registration.register_startup().await {
            Ok(()) => {}
            Err(e) if self.config.bittensor.common.network == "local" => {
                warn!(
                    "serve_axon failed on local network (non-fatal): {}. \
                     Use sudo_as on the local chain to pre-register the axon.",
                    e
                );
            }
            Err(e) => return Err(e),
        }

        // Log the discovered UID
        if let Some(uid) = self.chain_registration.get_discovered_uid().await {
            info!("Miner registered with discovered UID: {}", uid);
        } else {
            warn!("No UID discovered - miner may not be registered on chain");
        }

        let (shutdown_tx, shutdown_rx) = watch::channel(false);

        // One-shot validator discovery at startup
        let discovered = self.validator_discovery.run_discovery().await?;

        // Allow config to override the gRPC endpoint (for local dev with spoofed IPs)
        let grpc_endpoint = self
            .config
            .validator_assignment
            .grpc_endpoint_override
            .clone()
            .unwrap_or(discovered.grpc_endpoint);
        info!(
            grpc_endpoint = %grpc_endpoint,
            "Validator discovered, starting bid manager"
        );

        // Start bid manager (owns registration lifecycle: register, health checks)
        let bid_manager = self.bid_manager.clone();
        let bid_manager_shutdown_rx = shutdown_rx.clone();
        let bid_manager_handle = tokio::spawn(async move {
            if let Err(e) = bid_manager
                .run(grpc_endpoint, bid_manager_shutdown_rx)
                .await
            {
                error!("BidManager error: {:#}", e);
            }
        });

        info!("All miner services started successfully");

        // Wait for shutdown signal
        tokio::select! {
            _ = signal::ctrl_c() => {
                info!("Received shutdown signal, stopping miner...");
            }
            _ = bid_manager_handle => {
                error!("BidManager stopped unexpectedly");
            }
        }

        let _ = shutdown_tx.send(true);

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

    info!("Starting Cathedral Miner (UID: {})", state.miner_uid.as_u16());

    // Start all services
    state.start_services().await?;

    info!("Cathedral Miner stopped");
    Ok(())
}

/// Handle CLI commands
async fn handle_cli_command(command: Commands, config: &MinerConfig) -> Result<()> {
    match command {
        Commands::Config { config_cmd } => cli::handle_config_command(config_cmd, config).await,
        Commands::Status => {
            let db = RegistrationDb::new(&config.database).await?;
            cli::show_miner_status(config, db).await
        }
        Commands::Migrate => {
            let mut db_config = config.database.clone();
            db_config.run_migrations = true;
            let _db = RegistrationDb::new(&db_config).await?;
            println!("Database migrations completed successfully");
            Ok(())
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
