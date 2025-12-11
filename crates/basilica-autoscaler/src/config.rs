use std::time::Duration;

/// Autoscaler configuration loaded from environment variables
#[derive(Clone, Debug)]
pub struct AutoscalerConfig {
    pub reconcile: ReconcileConfig,
    pub basilica_api: BasilicaApiConfig,
    pub leader_election: LeaderElectionConfig,
    pub health: HealthConfig,
    pub metrics: MetricsServerConfig,
    pub ssh: SshConfig,
    pub network_validation: NetworkValidationConfig,
}

impl AutoscalerConfig {
    /// Load configuration from environment variables
    pub fn from_env() -> Self {
        Self {
            reconcile: ReconcileConfig::from_env(),
            basilica_api: BasilicaApiConfig::from_env(),
            leader_election: LeaderElectionConfig::from_env(),
            health: HealthConfig::from_env(),
            metrics: MetricsServerConfig::from_env(),
            ssh: SshConfig::from_env(),
            network_validation: NetworkValidationConfig::from_env(),
        }
    }
}

/// Reconciliation timing configuration
#[derive(Clone, Debug)]
pub struct ReconcileConfig {
    pub success_interval: Duration,
    pub error_interval: Duration,
    pub phase_check_interval: Duration,
}

impl ReconcileConfig {
    fn from_env() -> Self {
        Self {
            success_interval: parse_duration_env("BASILICA_AUTOSCALER_RECONCILE_SUCCESS_SECS", 60),
            error_interval: parse_duration_env("BASILICA_AUTOSCALER_RECONCILE_ERROR_SECS", 10),
            phase_check_interval: parse_duration_env("BASILICA_AUTOSCALER_PHASE_CHECK_SECS", 15),
        }
    }
}

/// Basilica API configuration
#[derive(Clone, Debug)]
pub struct BasilicaApiConfig {
    pub url: String,
    pub timeout: Duration,
}

impl BasilicaApiConfig {
    fn from_env() -> Self {
        Self {
            url: std::env::var("BASILICA_API_URL")
                .unwrap_or_else(|_| "https://api.basilica.ai".to_string()),
            timeout: parse_duration_env("BASILICA_API_TIMEOUT_SECS", 30),
        }
    }
}

/// Leader election configuration
#[derive(Clone, Debug)]
pub struct LeaderElectionConfig {
    pub enabled: bool,
    pub lease_name: String,
    pub lease_duration: Duration,
    pub renew_deadline: Duration,
    pub retry_period: Duration,
    pub max_consecutive_failures: u32,
}

impl LeaderElectionConfig {
    fn from_env() -> Self {
        Self {
            enabled: parse_bool_env("BASILICA_AUTOSCALER_LEADER_ELECTION_ENABLED", true),
            lease_name: std::env::var("BASILICA_AUTOSCALER_LEASE_NAME")
                .unwrap_or_else(|_| "basilica-autoscaler-leader".to_string()),
            lease_duration: parse_duration_env("BASILICA_AUTOSCALER_LEASE_DURATION_SECS", 15),
            renew_deadline: parse_duration_env("BASILICA_AUTOSCALER_RENEW_DEADLINE_SECS", 10),
            retry_period: parse_duration_env("BASILICA_AUTOSCALER_RETRY_PERIOD_SECS", 2),
            max_consecutive_failures: parse_u32_env("BASILICA_AUTOSCALER_MAX_LEADER_FAILURES", 5),
        }
    }
}

/// Health server configuration
#[derive(Clone, Debug)]
pub struct HealthConfig {
    pub port: u16,
    pub host: String,
}

impl HealthConfig {
    fn from_env() -> Self {
        Self {
            port: parse_u16_env("BASILICA_AUTOSCALER_HEALTH_PORT", 8080),
            host: std::env::var("BASILICA_AUTOSCALER_HEALTH_HOST")
                .unwrap_or_else(|_| "0.0.0.0".to_string()),
        }
    }
}

/// Metrics server configuration
#[derive(Clone, Debug)]
pub struct MetricsServerConfig {
    pub port: u16,
    pub host: String,
}

impl MetricsServerConfig {
    fn from_env() -> Self {
        Self {
            port: parse_u16_env("BASILICA_AUTOSCALER_METRICS_PORT", 9400),
            host: std::env::var("BASILICA_AUTOSCALER_METRICS_HOST")
                .unwrap_or_else(|_| "0.0.0.0".to_string()),
        }
    }
}

/// SSH configuration
#[derive(Clone, Debug)]
pub struct SshConfig {
    pub connection_timeout: Duration,
    pub execution_timeout: Duration,
    pub max_retries: u32,
    pub retry_delay: Duration,
    pub known_hosts_dir: String,
}

impl SshConfig {
    fn from_env() -> Self {
        Self {
            connection_timeout: parse_duration_env(
                "BASILICA_AUTOSCALER_SSH_CONNECT_TIMEOUT_SECS",
                30,
            ),
            execution_timeout: parse_duration_env("BASILICA_AUTOSCALER_SSH_EXEC_TIMEOUT_SECS", 300),
            max_retries: parse_u32_env("BASILICA_AUTOSCALER_SSH_MAX_RETRIES", 3),
            retry_delay: parse_duration_env("BASILICA_AUTOSCALER_SSH_RETRY_DELAY_SECS", 5),
            known_hosts_dir: std::env::var("BASILICA_AUTOSCALER_KNOWN_HOSTS_DIR")
                .unwrap_or_else(|_| "/var/lib/basilica-autoscaler/known_hosts".to_string()),
        }
    }
}

/// Network validation configuration
#[derive(Clone, Debug)]
pub struct NetworkValidationConfig {
    /// Control plane WireGuard IPs to ping during network validation
    pub control_plane_ips: Vec<String>,
}

impl NetworkValidationConfig {
    fn from_env() -> Self {
        let default_ips = "10.200.0.1,10.200.0.2,10.200.0.3".to_string();
        let ips_str = std::env::var("BASILICA_AUTOSCALER_CONTROL_PLANE_IPS").unwrap_or(default_ips);
        let control_plane_ips = ips_str
            .split(',')
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect();
        Self { control_plane_ips }
    }
}

/// Phase timeout configuration (seconds)
pub struct PhaseTimeouts;

impl PhaseTimeouts {
    pub const PROVISIONING: u64 = 600; // 10 min
    pub const CONFIGURING: u64 = 300; // 5 min
    pub const INSTALLING_WIREGUARD: u64 = 300; // 5 min
    pub const VALIDATING_NETWORK: u64 = 120; // 2 min
    pub const JOINING_CLUSTER: u64 = 600; // 10 min
    pub const WAITING_FOR_NODE: u64 = 300; // 5 min
    pub const DRAINING: u64 = 900; // 15 min (PDB wait)
    pub const TERMINATING: u64 = 120; // 2 min
}

fn parse_duration_env(key: &str, default_secs: u64) -> Duration {
    std::env::var(key)
        .ok()
        .and_then(|s| s.parse::<u64>().ok())
        .map(Duration::from_secs)
        .unwrap_or(Duration::from_secs(default_secs))
}

fn parse_u16_env(key: &str, default: u16) -> u16 {
    std::env::var(key)
        .ok()
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(default)
}

fn parse_u32_env(key: &str, default: u32) -> u32 {
    std::env::var(key)
        .ok()
        .and_then(|s| s.parse::<u32>().ok())
        .unwrap_or(default)
}

fn parse_bool_env(key: &str, default: bool) -> bool {
    std::env::var(key)
        .ok()
        .map(|s| s.eq_ignore_ascii_case("true") || s == "1")
        .unwrap_or(default)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_config_is_sensible() {
        let config = AutoscalerConfig::from_env();
        assert_eq!(config.reconcile.success_interval, Duration::from_secs(60));
        assert_eq!(config.reconcile.error_interval, Duration::from_secs(10));
        assert_eq!(config.health.port, 8080);
        assert_eq!(config.metrics.port, 9400);
        assert!(config.leader_election.enabled);
    }

    #[test]
    fn phase_timeouts_are_reasonable() {
        assert_eq!(PhaseTimeouts::PROVISIONING, 600);
        assert_eq!(PhaseTimeouts::DRAINING, 900);
    }
}
