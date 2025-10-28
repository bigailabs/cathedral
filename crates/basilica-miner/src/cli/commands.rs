use clap::Subcommand;

#[derive(Subcommand, Debug)]
pub enum Commands {
    /// Configuration management commands
    Config {
        #[command(subcommand)]
        config_cmd: ConfigCommand,
    },
    /// Show miner status and statistics
    Status,
    /// Run database migrations
    Migrate,
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
}
