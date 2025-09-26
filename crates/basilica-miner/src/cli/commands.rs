use clap::Subcommand;

#[derive(Subcommand, Debug)]
pub enum Commands {
    /// Validator management commands
    Validator {
        #[command(subcommand)]
        validator_cmd: ValidatorCommand,
    },
    /// Service management commands
    Service {
        #[command(subcommand)]
        service_cmd: ServiceCommand,
    },
    /// Database management commands
    Database {
        #[command(subcommand)]
        database_cmd: DatabaseCommand,
    },
    /// Configuration management commands
    Config {
        #[command(subcommand)]
        config_cmd: ConfigCommand,
    },
    /// Show miner status and statistics
    Status,
    /// Run database migrations
    Migrate,
    /// Deploy nodes to remote machines
    DeployNodes {
        /// Only show what would be deployed without actually deploying
        #[arg(long)]
        dry_run: bool,
        /// Deploy to specific machine IDs only (comma-separated)
        #[arg(long)]
        only_machines: Option<String>,
        /// Skip deployment and only check status
        #[arg(long)]
        status_only: bool,
    },
}

/// Validator management subcommands
#[derive(Subcommand, Debug)]
pub enum ValidatorCommand {
    /// List recent validator interactions
    List {
        /// Number of recent interactions to show
        #[arg(short, long, default_value = "100")]
        limit: i64,
    },

    /// Show SSH access grants for a validator
    ShowAccess {
        /// Validator hotkey
        hotkey: String,
    },
}

/// Service management subcommands
#[derive(Subcommand, Debug)]
pub enum ServiceCommand {
    /// Start the miner service
    Start,

    /// Stop the miner service
    Stop,

    /// Restart the miner service
    Restart,

    /// Show service status
    Status,

    /// Reload service configuration
    Reload,
}

/// Database management subcommands
#[derive(Subcommand, Debug)]
pub enum DatabaseCommand {
    /// Backup the database
    Backup {
        /// Backup file path
        path: String,
    },

    /// Restore database from backup
    Restore {
        /// Backup file path to restore from
        path: String,
    },

    /// Show database statistics
    Stats,

    /// Clean up old database records
    Cleanup {
        /// Number of days to keep records (default: 30)
        #[arg(short, long, default_value = "30")]
        days: u32,
    },

    /// Vacuum database to reclaim space
    Vacuum,

    /// Check database integrity
    Integrity,
}

/// Configuration management subcommands
#[derive(Subcommand, Debug)]
pub enum ConfigCommand {
    /// Validate configuration file
    Validate {
        /// Configuration file path to validate (default: current config)
        #[arg(short, long)]
        path: Option<String>,
    },

    /// Show current configuration
    Show {
        /// Show sensitive fields (default: masked)
        #[arg(long)]
        show_sensitive: bool,
    },

    /// Reload configuration (test only)
    Reload,

    /// Compare configurations
    Diff {
        /// Path to configuration file to compare with
        other_path: String,
    },

    /// Export configuration in different formats
    Export {
        /// Export format (toml, json, yaml)
        #[arg(short, long, default_value = "toml")]
        format: String,
        /// Output file path
        path: String,
    },
}
