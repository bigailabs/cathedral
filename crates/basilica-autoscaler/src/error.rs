use thiserror::Error;

/// Result type alias for autoscaler operations
pub type Result<T> = std::result::Result<T, AutoscalerError>;

/// Autoscaler-specific errors
#[derive(Debug, Error)]
pub enum AutoscalerError {
    #[error("SSH connection failed to {host}: {reason}")]
    SshConnection { host: String, reason: String },

    #[error(
        "SSH command execution failed: command='{command}', exit_code={exit_code}, stderr={stderr}"
    )]
    SshExecution {
        command: String,
        exit_code: u32,
        stderr: String,
    },

    #[error("SSH authentication failed: {0}")]
    SshAuthentication(String),

    #[error("WireGuard setup failed: {0}")]
    WireGuardSetup(String),

    #[error("WireGuard peer update failed: {0}")]
    WireGuardPeerUpdate(String),

    #[error("K3s installation failed: {0}")]
    K3sInstall(String),

    #[error("K3s agent failed to start: {0}")]
    K3sAgentStart(String),

    #[error("Flannel interface not ready on node {node} after {attempts} attempts")]
    FlannelTimeout { node: String, attempts: u32 },

    #[error("Node {node} failed identity verification: {details}")]
    IdentityVerification { node: String, details: String },

    #[error("Phase timeout: {phase} exceeded {timeout_secs}s")]
    PhaseTimeout { phase: String, timeout_secs: u64 },

    #[error("Scale-down not allowed: node {node} is not autoscaler-managed")]
    ScaleDownNotAllowed { node: String },

    #[error("Node {node_id} already exists. {hint}")]
    NodeAlreadyExists { node_id: String, hint: String },

    #[error("Node adoption failed: {reason}")]
    AdoptionFailed { reason: String },

    #[error("Node password generation failed: {0}")]
    PasswordGeneration(String),

    #[error("Secret not found: {0}")]
    SecretNotFound(String),

    #[error("Secret key not found: {key} in {namespace}/{name}")]
    SecretKeyNotFound {
        key: String,
        namespace: String,
        name: String,
    },

    #[error("Node not found: {0}")]
    NodeNotFound(String),

    #[error("NodePool not found: {namespace}/{name}")]
    NodePoolNotFound { namespace: String, name: String },

    #[error("Invalid NodePool configuration: {0}")]
    InvalidConfiguration(String),

    #[error("Network validation failed: {0}")]
    NetworkValidation(String),

    #[error("Connectivity check failed to {target}: {reason}")]
    ConnectivityCheck { target: String, reason: String },

    #[error("API registration failed: {0}")]
    ApiRegistration(String),

    #[error("Secure Cloud API error: {0}")]
    SecureCloudApi(String),

    #[error("Rental start failed: {0}")]
    RentalStart(String),

    #[error("Rental stop failed: {0}")]
    RentalStop(String),

    #[error("No GPU offering found matching requirements: {gpu_count} GPUs, models: {models:?}, min_memory: {min_memory_gb:?}GB")]
    NoMatchingOffering {
        gpu_count: u32,
        models: Vec<String>,
        min_memory_gb: Option<u32>,
    },

    #[error("IP exhaustion: no available IPs in range")]
    IpExhaustion,

    #[error("Drain timeout exceeded for node {node}")]
    DrainTimeout { node: String },

    #[error("Pod eviction failed: {namespace}/{name}: {reason}")]
    PodEviction {
        namespace: String,
        name: String,
        reason: String,
    },

    #[error("Cleanup failed: {step}: {reason}")]
    CleanupFailed { step: String, reason: String },

    #[error("Leader election failed: {0}")]
    LeaderElection(String),

    #[error("Controller runtime failed: {0}")]
    Runtime(String),

    #[error("Circuit breaker open: {0}")]
    CircuitBreakerOpen(String),

    #[error("API timeout: {operation} exceeded {timeout_secs}s")]
    ApiTimeout {
        operation: String,
        timeout_secs: u64,
    },

    #[error("Kubernetes API error: {0}")]
    KubeApi(#[from] kube::Error),

    #[error("JSON serialization error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),

    #[error("HTTP request error: {0}")]
    Http(#[from] reqwest::Error),

    #[error("Internal error: {0}")]
    Internal(String),
}

impl AutoscalerError {
    /// Returns true if the error is retryable
    pub fn is_retryable(&self) -> bool {
        matches!(
            self,
            Self::SshConnection { .. }
                | Self::SshExecution { .. }
                | Self::NetworkValidation(_)
                | Self::KubeApi(_)
                | Self::Http(_)
                | Self::SecureCloudApi(_)
                | Self::LeaderElection(_)
                | Self::NoMatchingOffering { .. }
                | Self::ApiTimeout { .. }
        )
    }

    /// Returns true if the error indicates a permanent failure
    pub fn is_permanent(&self) -> bool {
        matches!(
            self,
            Self::InvalidConfiguration(_)
                | Self::NodeAlreadyExists { .. }
                | Self::ScaleDownNotAllowed { .. }
                | Self::IpExhaustion
        )
    }
}
