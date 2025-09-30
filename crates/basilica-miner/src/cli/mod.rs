//! # CLI Module
//!
//! Complete command-line interface for miner operations with production-ready
//! operational commands for service management, database operations, and configuration.

use anyhow::Result;
use tracing::error;

use crate::config::MinerConfig;
use crate::persistence::RegistrationDb;

mod args;
mod commands;
pub mod handlers;

pub use args::*;
pub use commands::*;

/// Handle validator management commands
pub async fn handle_validator_command(command: ValidatorCommand, db: RegistrationDb) -> Result<()> {
    match command {
        ValidatorCommand::List { limit } => list_validator_interactions(db, limit).await,
        ValidatorCommand::ShowAccess { hotkey } => show_validator_ssh_access(db, hotkey).await,
    }
}

/// Handle service management commands
pub async fn handle_service_command(command: ServiceCommand, config: &MinerConfig) -> Result<()> {
    let operation = match command {
        ServiceCommand::Start => handlers::ServiceOperation::Start,
        ServiceCommand::Stop => handlers::ServiceOperation::Stop,
        ServiceCommand::Restart => handlers::ServiceOperation::Restart,
        ServiceCommand::Status => handlers::ServiceOperation::Status,
        ServiceCommand::Reload => handlers::ServiceOperation::Reload,
    };

    handlers::handle_service_command(operation, config).await
}

/// Handle database management commands
pub async fn handle_database_command(command: DatabaseCommand, config: &MinerConfig) -> Result<()> {
    let operation = match command {
        DatabaseCommand::Backup { path } => handlers::DatabaseOperation::Backup { path },
        DatabaseCommand::Restore { path } => handlers::DatabaseOperation::Restore { path },
        DatabaseCommand::Stats => handlers::DatabaseOperation::Stats,
        DatabaseCommand::Cleanup { days } => {
            handlers::DatabaseOperation::Cleanup { days: Some(days) }
        }
        DatabaseCommand::Vacuum => handlers::DatabaseOperation::Vacuum,
        DatabaseCommand::Integrity => handlers::DatabaseOperation::Integrity,
    };

    handlers::handle_database_command(operation, config).await
}

/// Handle configuration management commands
pub async fn handle_config_command(command: ConfigCommand, config: &MinerConfig) -> Result<()> {
    let operation = match command {
        ConfigCommand::Validate { path } => handlers::ConfigOperation::Validate { path },
        ConfigCommand::Show { show_sensitive } => {
            handlers::ConfigOperation::Show { show_sensitive }
        }
        ConfigCommand::Reload => handlers::ConfigOperation::Reload,
        ConfigCommand::Diff { other_path } => handlers::ConfigOperation::Diff { other_path },
        ConfigCommand::Export { format, path } => {
            let config_format = match format.as_str() {
                "json" => handlers::ConfigFormat::Json,
                "yaml" => handlers::ConfigFormat::Yaml,
                _ => handlers::ConfigFormat::Toml,
            };
            handlers::ConfigOperation::Export {
                format: config_format,
                path,
            }
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
    println!("Validator Comms: {:?}", config.validator_comms.auth.method);
    println!("Note: UID will be discovered from chain on startup");
    println!();

    // Show configured nodes
    println!("Configured Nodes: {}", config.node_management.nodes.len());
    for node in &config.node_management.nodes {
        let node_endpoint = format!("{}:{}", node.host, node.port);
        let node_id = if node.node_id.is_empty() {
            format!("node-{}", node_endpoint)
        } else {
            node.node_id.clone()
        };
        println!(
            "  - {} @ {} (ssh://{}@{})",
            node_id, node_endpoint, node.username, node.host
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

    // Show health status summary
    if let Ok(health_records) = db.get_all_node_health().await {
        let healthy_count = health_records.iter().filter(|h| h.is_healthy).count();
        println!("Healthy Nodes: {}/{}", healthy_count, health_records.len());
    }

    Ok(())
}

/// List recent validator interactions
async fn list_validator_interactions(db: RegistrationDb, limit: i64) -> Result<()> {
    let interactions = db.get_recent_validator_interactions(limit).await?;

    if interactions.is_empty() {
        println!("No validator interactions found");
        return Ok(());
    }

    println!("=== Recent Validator Interactions ===");
    println!(
        "{:<44} {:<20} {:<10} {:<20}",
        "Validator", "Type", "Success", "Time"
    );
    println!("{}", "-".repeat(100));

    for interaction in interactions {
        println!(
            "{:<44} {:<20} {:<10} {:<20}",
            interaction.validator_hotkey,
            interaction.interaction_type,
            if interaction.success { "Yes" } else { "No" },
            interaction.created_at.format("%Y-%m-%d %H:%M:%S")
        );

        if let Some(details) = &interaction.details {
            println!("  Details: {details}");
        }
    }

    Ok(())
}

/// Show SSH access grants for a validator
async fn show_validator_ssh_access(db: RegistrationDb, hotkey: String) -> Result<()> {
    let grants = db.get_active_ssh_grants(&hotkey).await?;

    if grants.is_empty() {
        println!("No active SSH access grants found for validator {hotkey}");
        return Ok(());
    }

    println!("=== SSH Access Grants for {hotkey} ===");
    println!(
        "{:<10} {:<30} {:<20} {:<10}",
        "Grant ID", "Nodes", "Granted At", "Active"
    );
    println!("{}", "-".repeat(80));

    for grant in grants {
        let node_ids: Vec<String> = serde_json::from_str(&grant.node_ids).unwrap_or_default();
        let nodes = node_ids.join(", ");

        println!(
            "{:<10} {:<30} {:<20} {:<10}",
            grant.id,
            if nodes.len() > 30 {
                &nodes[..30]
            } else {
                &nodes
            },
            grant.granted_at.format("%Y-%m-%d %H:%M:%S"),
            if grant.is_active { "Yes" } else { "No" }
        );

        if let Some(expires) = grant.expires_at {
            println!("  Expires: {}", expires.format("%Y-%m-%d %H:%M:%S"));
        }
    }

    Ok(())
}
