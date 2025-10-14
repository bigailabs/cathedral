//! # CLI Module
//!
//! Complete command-line interface for miner operations focused on configuration tasks.

use anyhow::Result;
use tracing::error;

use crate::config::MinerConfig;
use crate::persistence::RegistrationDb;

mod args;
mod commands;
pub mod handlers;

pub use args::*;
pub use commands::*;

/// Handle configuration management commands
pub async fn handle_config_command(command: ConfigCommand, config: &MinerConfig) -> Result<()> {
    let operation = match command {
        ConfigCommand::Validate { path } => handlers::ConfigOperation::Validate { path },
        ConfigCommand::Show { show_sensitive } => {
            handlers::ConfigOperation::Show { show_sensitive }
        }
    };

    handlers::handle_config_command(operation, config).await
}

/// Show miner status
pub async fn show_miner_status(config: &MinerConfig, _db: RegistrationDb) -> Result<()> {
    println!("=== Basilca Miner Status ===");
    println!("Network: {}", config.bittensor.common.network);
    println!("Hotkey: {}", config.bittensor.common.hotkey_name);
    println!("Netuid: {}", config.bittensor.common.netuid);
    println!("Axon Port: {}", config.bittensor.axon_port);
    println!("Note: UID will be discovered from chain on startup");
    println!();

    // Show configured nodes (IDs will be auto-generated on startup)
    println!("Configured Nodes: {}", config.node_management.nodes.len());
    for node in &config.node_management.nodes {
        let node_endpoint = format!("{}:{}", node.host, node.port);
        println!(
            "  - ssh://{}@{} (ID will be auto-generated from credentials)",
            node.username, node_endpoint
        );
    }
    println!();

    // Connect to database to get stats
    let db = RegistrationDb::new(&config.database).await?;

    match db.health_check().await {
        Ok(_) => println!("Database: ✓ Connected"),
        Err(e) => {
            error!("Database connection failed: {}", e);
            println!("Database: ✗ Failed to connect");
        }
    }

    Ok(())
}
