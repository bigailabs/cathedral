use basilica_common::types::GpuCategory;
use basilica_sdk::types::RentalState;
use clap::{Subcommand, ValueEnum, ValueHint};
use std::path::PathBuf;

use crate::handlers::gpu_rental::GpuTarget;

/// CLI wrapper for ComputeCategory to implement ValueEnum
#[derive(Debug, Clone, Copy, ValueEnum)]
pub enum ComputeCategoryArg {
    #[value(name = "secure-cloud", alias = "secure", alias = "secure_cloud")]
    SecureCloud,
    #[value(
        name = "community-cloud",
        alias = "community",
        alias = "community_cloud"
    )]
    CommunityCloud,
}

impl From<ComputeCategoryArg> for basilica_common::types::ComputeCategory {
    fn from(arg: ComputeCategoryArg) -> Self {
        match arg {
            ComputeCategoryArg::SecureCloud => basilica_common::types::ComputeCategory::SecureCloud,
            ComputeCategoryArg::CommunityCloud => {
                basilica_common::types::ComputeCategory::CommunityCloud
            }
        }
    }
}

/// Main CLI commands
#[derive(Subcommand, Debug, Clone)]
pub enum Commands {
    /// List available GPU resources
    #[command(alias = "list")]
    Ls {
        /// Filter by GPU category (e.g., 'h100', 'h200', 'b200') (optional)
        gpu_type: Option<GpuCategory>,

        /// Compute source: 'secure-cloud' or 'community-cloud'
        #[arg(long, value_name = "TYPE")]
        compute: Option<ComputeCategoryArg>,

        #[command(flatten)]
        filters: ListFilters,
    },

    /// Provision and start GPU instances
    #[command(alias = "start")]
    Up {
        /// GPU category to filter by (e.g., 'h100', 'a100', 'b200') (optional)
        target: Option<GpuTarget>,

        /// Compute source: 'secure-cloud' or 'community-cloud'
        #[arg(long, value_name = "TYPE")]
        compute: Option<ComputeCategoryArg>,

        #[command(flatten)]
        options: UpOptions,
    },

    /// List active rentals and their status
    Ps {
        /// Compute source: 'secure-cloud' or 'community-cloud'
        #[arg(long, value_name = "TYPE")]
        compute: Option<ComputeCategoryArg>,

        #[command(flatten)]
        filters: PsFilters,
    },

    /// Check instance status
    Status {
        /// Rental UUID (optional)
        target: Option<String>,
    },

    /// View instance logs
    Logs {
        /// Rental UUID (optional)
        target: Option<String>,

        #[command(flatten)]
        options: LogsOptions,
    },

    /// Terminate instance
    #[command(alias = "stop")]
    Down {
        /// Rental UUID to terminate (optional)
        target: Option<String>,

        /// Compute source: 'secure-cloud' or 'community-cloud'
        #[arg(long, value_name = "TYPE")]
        compute: Option<ComputeCategoryArg>,

        /// Stop all active rentals
        #[arg(long, conflicts_with = "target")]
        all: bool,
    },

    /// Restart instance container
    Restart {
        /// Rental UUID to restart (optional)
        target: Option<String>,
    },

    /// Execute commands on instances
    Exec {
        /// Command to execute
        command: String,

        /// Rental UUID (optional)
        #[arg(long)]
        target: Option<String>,
    },

    /// SSH into instances
    #[command(alias = "connect")]
    Ssh {
        /// Rental UUID (optional)
        target: Option<String>,

        #[command(flatten)]
        options: SshOptions,
    },

    /// Copy files to/from instances
    Cp {
        /// Source path (local or remote)
        #[arg(value_hint = ValueHint::AnyPath)]
        source: String,

        /// Destination path (local or remote)
        #[arg(value_hint = ValueHint::AnyPath)]
        destination: String,
    },

    /// Log in to Basilica
    Login {
        /// Use device authorization flow (for WSL, SSH, containers)
        #[arg(long)]
        device_code: bool,
    },

    /// Log out of Basilica
    Logout,

    /// Test authentication token
    #[cfg(debug_assertions)]
    TestAuth {
        /// Test against Basilica API instead of Auth0
        #[arg(long)]
        api: bool,
    },

    /// Tokens management commands
    Tokens {
        #[command(subcommand)]
        action: TokenAction,
    },

    /// SSH keys management commands
    SshKeys {
        #[command(subcommand)]
        action: SshKeyAction,
    },

    /// Fund your account with Bittensor TAO
    Fund {
        #[command(subcommand)]
        action: Option<FundAction>,

        /// Output as JSON
        #[arg(long, global = true)]
        json: bool,
    },

    /// Check your account balance
    Balance {
        /// Output as JSON
        #[arg(long, global = true)]
        json: bool,
    },

    /// Upgrade the Basilica CLI to a newer version
    Upgrade {
        /// Specific version to upgrade to (e.g., "0.5.4")
        #[arg(long)]
        version: Option<String>,

        /// Check for updates without installing
        #[arg(long)]
        dry_run: bool,
    },

    /// Deploy applications to Basilica
    #[command(name = "deploy", visible_alias = "d")]
    Deploy(Box<DeployCommand>),
}

/// Fund management actions
#[derive(Subcommand, Debug, Clone)]
pub enum FundAction {
    /// List deposit history
    List {
        /// Limit number of results (default: 50)
        #[arg(long, default_value = "50")]
        limit: u32,

        /// Offset for pagination (default: 0)
        #[arg(long, default_value = "0")]
        offset: u32,
    },
}

/// Token management actions
#[derive(Subcommand, Debug, Clone)]
pub enum TokenAction {
    /// Create a new API key
    Create {
        /// Name for the API key (will prompt if not provided)
        name: Option<String>,
    },

    /// List all API keys
    List,

    /// Revoke an API key
    Revoke {
        /// Name of the API key to revoke (will prompt if not provided)
        name: Option<String>,

        /// Skip confirmation prompt
        #[arg(long, short = 'y')]
        yes: bool,
    },
}

/// SSH key management actions
#[derive(Subcommand, Debug, Clone)]
pub enum SshKeyAction {
    /// Add a new SSH key
    Add {
        /// Name for the SSH key (will prompt if not provided)
        #[arg(short, long)]
        name: Option<String>,

        /// Path to SSH public key file (default: auto-detect from ~/.ssh/)
        #[arg(short, long, value_hint = ValueHint::FilePath)]
        file: Option<PathBuf>,
    },

    /// List registered SSH keys
    List,

    /// Delete the registered SSH key
    Delete {
        /// Skip confirmation prompt
        #[arg(long, short = 'y')]
        yes: bool,
    },
}

impl Commands {
    /// Check if this command requires authentication
    pub fn requires_auth(&self) -> bool {
        match self {
            // GPU rental commands require authentication
            Commands::Ls { .. }
            | Commands::Up { .. }
            | Commands::Ps { .. }
            | Commands::Status { .. }
            | Commands::Logs { .. }
            | Commands::Down { .. }
            | Commands::Restart { .. }
            | Commands::Exec { .. }
            | Commands::Ssh { .. }
            | Commands::Cp { .. }
            | Commands::Tokens { .. }
            | Commands::SshKeys { .. }
            | Commands::Fund { .. }
            | Commands::Balance { .. }
            | Commands::Deploy(_) => true,

            // Authentication commands don't require auth
            Commands::Login { .. } | Commands::Logout | Commands::Upgrade { .. } => false,

            // Test auth command requires authentication
            #[cfg(debug_assertions)]
            Commands::TestAuth { .. } => true,
        }
    }
}

/// Filters for listing GPUs
#[derive(clap::Args, Debug, Clone)]
pub struct ListFilters {
    /// Minimum GPU count
    #[arg(long)]
    pub gpu_min: Option<u32>,

    /// Maximum GPU count
    #[arg(long)]
    pub gpu_max: Option<u32>,

    /// Maximum price per hour
    #[arg(long)]
    pub price_max: Option<f64>,

    /// Minimum memory in GB
    #[arg(long)]
    pub memory_min: Option<u32>,

    /// Filter by country code (e.g., US, UK, DE)
    #[arg(long)]
    pub country: Option<String>,
}

/// Options for provisioning instances
#[derive(clap::Args, Debug, Clone)]
pub struct UpOptions {
    /// Exact GPU count required
    #[arg(long)]
    pub gpu_count: Option<u32>,

    /// Docker image to run (community cloud only)
    #[arg(long)]
    pub image: Option<String>,

    /// Environment variables (KEY=VALUE) (community cloud only)
    #[arg(long)]
    pub env: Vec<String>,

    /// Instance name
    #[arg(long)]
    pub name: Option<String>,

    /// SSH public key file path
    #[arg(long, value_hint = ValueHint::FilePath)]
    pub ssh_key: Option<PathBuf>,

    /// Port mappings (host:container) (community cloud only)
    #[arg(long)]
    pub ports: Vec<String>,

    /// CPU cores (community cloud only)
    #[arg(long)]
    pub cpu_cores: Option<f64>,

    /// Memory in MB (community cloud only)
    #[arg(long)]
    pub memory_mb: Option<i64>,

    /// Storage in MB (community cloud only)
    #[arg(long)]
    pub storage_mb: Option<i64>,

    /// Command to run (community cloud only)
    #[arg(long)]
    pub command: Vec<String>,

    /// Filter by country code (e.g., US, UK, DE)
    #[arg(long)]
    pub country: Option<String>,

    /// Disable SSH access (faster startup)
    #[arg(long)]
    pub no_ssh: bool,

    /// Create rental in detached mode (don't auto-connect via SSH)
    #[arg(short = 'd', long)]
    pub detach: bool,
}

/// Filters for listing active rentals
#[derive(clap::Args, Debug, Clone)]
pub struct PsFilters {
    /// Filter by status (defaults to 'active' if not specified)
    #[arg(long, value_enum)]
    pub status: Option<RentalState>,

    /// Filter by GPU type
    #[arg(long)]
    pub gpu_type: Option<String>,

    /// Minimum GPU count
    #[arg(long)]
    pub min_gpu_count: Option<u32>,

    /// Show rental history instead of active rentals
    #[arg(long)]
    pub history: bool,
}

/// Options for viewing logs
#[derive(clap::Args, Debug, Clone)]
pub struct LogsOptions {
    /// Follow logs in real-time
    #[arg(short, long)]
    pub follow: bool,

    /// Number of lines to tail
    #[arg(long)]
    pub tail: Option<u32>,
}

/// Options for SSH connections
#[derive(clap::Args, Debug, Clone)]
pub struct SshOptions {
    /// Local port forwarding (local_port:remote_host:remote_port)
    #[arg(short = 'L', long)]
    pub local_forward: Vec<String>,

    /// Remote port forwarding (remote_port:local_host:local_port)
    #[arg(short = 'R', long)]
    pub remote_forward: Vec<String>,
}

// ============================================================================
// Deploy Command Definitions
// ============================================================================

/// Deploy command with subcommands and options
#[derive(clap::Parser, Debug, Clone)]
pub struct DeployCommand {
    #[command(subcommand)]
    pub action: Option<DeployAction>,

    /// Source file or Docker image to deploy
    #[arg(value_name = "SOURCE")]
    pub source: Option<String>,

    #[command(flatten)]
    pub naming: NamingOptions,

    #[command(flatten)]
    pub resources: ResourceOptions,

    #[command(flatten)]
    pub gpu: GpuOptions,

    #[command(flatten)]
    pub storage: StorageOptions,

    #[command(flatten)]
    pub health: HealthCheckOptions,

    #[command(flatten)]
    pub networking: NetworkingOptions,

    #[command(flatten)]
    pub lifecycle: LifecycleOptions,

    /// Output as JSON
    #[arg(long, global = true)]
    pub json: bool,

    /// Show detailed deployment phases during progress
    #[arg(long, global = true)]
    pub show_phases: bool,
}

/// Naming and identification options
#[derive(clap::Args, Debug, Clone, Default)]
pub struct NamingOptions {
    /// Deployment name (auto-generated if not specified)
    #[arg(short, long)]
    pub name: Option<String>,

    /// Docker image (default: python:3.11-slim for .py files)
    #[arg(long)]
    pub image: Option<String>,

    /// Number of replicas (default: 1)
    #[arg(long, default_value = "1")]
    pub replicas: u32,
}

/// Resource allocation options (limits and requests)
#[derive(clap::Args, Debug, Clone)]
pub struct ResourceOptions {
    /// CPU limit (e.g., "500m", "2")
    #[arg(long, default_value = "500m")]
    pub cpu: String,

    /// Memory limit (e.g., "512Mi", "2Gi")
    #[arg(long, default_value = "512Mi")]
    pub memory: String,

    /// CPU request (defaults to cpu limit)
    #[arg(long)]
    pub cpu_request: Option<String>,

    /// Memory request (defaults to memory limit)
    #[arg(long)]
    pub memory_request: Option<String>,
}

impl Default for ResourceOptions {
    fn default() -> Self {
        Self {
            cpu: "500m".to_string(),
            memory: "512Mi".to_string(),
            cpu_request: None,
            memory_request: None,
        }
    }
}

/// GPU configuration options
#[derive(clap::Args, Debug, Clone, Default)]
pub struct GpuOptions {
    /// Number of GPUs (1-8)
    #[arg(long)]
    pub gpu: Option<u32>,

    /// GPU model requirements (e.g., "A100", "H100")
    #[arg(long)]
    pub gpu_model: Vec<String>,

    /// Minimum CUDA version (e.g., "12.0")
    #[arg(long)]
    pub cuda_version: Option<String>,

    /// Minimum GPU memory in GB
    #[arg(long)]
    pub gpu_memory_gb: Option<u32>,

    /// GPU vendor (nvidia, amd)
    #[arg(long)]
    pub gpu_vendor: Option<String>,

    /// Node selector labels (format: key=value, can be specified multiple times)
    #[arg(long, value_name = "KEY=VALUE")]
    pub node_selector: Vec<String>,

    /// Preferred node affinity labels (soft constraint, format: key=value)
    #[arg(long, value_name = "KEY=VALUE")]
    pub prefer_node: Vec<String>,

    /// Required node affinity labels (hard constraint, format: key=value)
    #[arg(long, value_name = "KEY=VALUE")]
    pub require_node: Vec<String>,
}

/// Storage configuration options
#[derive(clap::Args, Debug, Clone)]
pub struct StorageOptions {
    /// Enable persistent storage
    #[arg(long)]
    pub storage: bool,

    /// Storage mount path (default: /data)
    #[arg(long, default_value = "/data")]
    pub storage_path: String,

    /// Storage cache size in MB (default: 2048)
    #[arg(long, default_value = "2048")]
    pub storage_cache_mb: usize,

    /// Storage sync interval in ms (default: 1000)
    #[arg(long, default_value = "1000")]
    pub storage_sync_ms: u64,
}

impl Default for StorageOptions {
    fn default() -> Self {
        Self {
            storage: false,
            storage_path: "/data".to_string(),
            storage_cache_mb: 2048,
            storage_sync_ms: 1000,
        }
    }
}

/// Health check configuration options
#[derive(clap::Args, Debug, Clone, Default)]
pub struct HealthCheckOptions {
    /// HTTP path for liveness probe
    #[arg(long)]
    pub liveness_path: Option<String>,

    /// HTTP path for readiness probe
    #[arg(long)]
    pub readiness_path: Option<String>,

    /// HTTP path for startup probe (delays liveness/readiness until app is ready)
    #[arg(long)]
    pub startup_path: Option<String>,

    /// Shorthand for all probes (same path)
    #[arg(long)]
    pub health_path: Option<String>,

    /// Port for health probes (defaults to primary container port)
    #[arg(long)]
    pub health_port: Option<u16>,

    /// Initial delay before liveness/readiness probes start (seconds)
    #[arg(long, default_value = "30")]
    pub health_initial_delay: u32,

    /// Probe interval (seconds)
    #[arg(long, default_value = "10")]
    pub health_period: u32,

    /// Probe timeout (seconds)
    #[arg(long, default_value = "5")]
    pub health_timeout: u32,

    /// Failure threshold before restart
    #[arg(long, default_value = "3")]
    pub health_failure_threshold: u32,

    /// Startup probe failure threshold (higher for slow-starting apps)
    #[arg(long, default_value = "30")]
    pub startup_failure_threshold: u32,
}

/// Networking options
#[derive(clap::Args, Debug, Clone)]
pub struct NetworkingOptions {
    /// Container ports (format: PORT[:NAME], e.g., 8000:http, 9090:metrics)
    #[arg(short, long, value_name = "PORT[:NAME]", default_value = "8000")]
    pub port: Vec<String>,

    /// Create public URL (default: true)
    #[arg(long, default_value = "true")]
    pub public: bool,

    /// Environment variables (KEY=VALUE)
    #[arg(short, long, value_name = "KEY=VALUE")]
    pub env: Vec<String>,

    /// Additional pip packages to install
    #[arg(long, num_args = 1..)]
    pub pip: Vec<String>,
}

impl Default for NetworkingOptions {
    fn default() -> Self {
        Self {
            port: vec!["8000".to_string()],
            public: true,
            env: vec![],
            pip: vec![],
        }
    }
}

/// Lifecycle options
#[derive(clap::Args, Debug, Clone)]
pub struct LifecycleOptions {
    /// Time-to-live in seconds (60-604800, auto-delete after expiry)
    #[arg(long)]
    pub ttl: Option<u32>,

    /// Deployment timeout in seconds
    #[arg(long, default_value = "300")]
    pub timeout: u32,

    /// Don't wait for deployment to be ready
    #[arg(long)]
    pub detach: bool,

    /// Termination grace period in seconds
    #[arg(long, default_value = "30")]
    pub grace_period: u32,

    /// Skip GPU resource correlation validation (use with caution)
    #[arg(long)]
    pub skip_gpu_validation: bool,
}

impl Default for LifecycleOptions {
    fn default() -> Self {
        Self {
            ttl: None,
            timeout: 300,
            detach: false,
            grace_period: 30,
            skip_gpu_validation: false,
        }
    }
}

/// Deploy subcommands
#[derive(Subcommand, Debug, Clone)]
pub enum DeployAction {
    /// List all deployments
    #[command(name = "ls", visible_alias = "list")]
    List,

    /// Get deployment status
    #[command(name = "status", visible_alias = "get")]
    Status {
        /// Deployment name
        name: String,
    },

    /// Stream deployment logs
    #[command(name = "logs")]
    Logs {
        /// Deployment name
        name: String,
        /// Follow log output
        #[arg(short, long)]
        follow: bool,
        /// Number of lines to show from end
        #[arg(long)]
        tail: Option<u32>,
    },

    /// Delete a deployment
    #[command(name = "delete", visible_alias = "rm")]
    Delete {
        /// Deployment name
        name: String,
        /// Skip confirmation
        #[arg(short, long)]
        yes: bool,
    },

    /// Scale deployment replicas
    #[command(name = "scale")]
    Scale {
        /// Deployment name
        name: String,
        /// Number of replicas
        #[arg(long)]
        replicas: u32,
    },
}
