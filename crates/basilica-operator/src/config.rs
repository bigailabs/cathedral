//! Operator configuration module.
//!
//! Provides configuration structs with environment variable support for all
//! configurable timeouts and settings.

use std::time::Duration;

/// Configuration for reconciliation intervals.
#[derive(Debug, Clone)]
pub struct ReconcileConfig {
    /// Success requeue interval for controllers (default: 30s)
    pub success_interval: Duration,
    /// Error requeue interval for controllers (default: 10s)
    pub error_interval: Duration,
    /// Node profile reconcile interval (default: 60s)
    pub node_profile_interval: Duration,
}

impl Default for ReconcileConfig {
    fn default() -> Self {
        Self {
            success_interval: Duration::from_secs(30),
            error_interval: Duration::from_secs(10),
            node_profile_interval: Duration::from_secs(60),
        }
    }
}

impl ReconcileConfig {
    /// Load configuration from environment variables.
    /// Falls back to defaults if variables are not set or invalid.
    pub fn from_env() -> Self {
        let success_interval = parse_duration_env("BASILICA_OPERATOR_RECONCILE_SUCCESS_SECS", 30);
        let error_interval = parse_duration_env("BASILICA_OPERATOR_RECONCILE_ERROR_SECS", 10);
        let node_profile_interval =
            parse_duration_env("BASILICA_OPERATOR_NODE_PROFILE_INTERVAL_SECS", 60);

        Self {
            success_interval,
            error_interval,
            node_profile_interval,
        }
    }
}

/// Configuration for Kubernetes API rate limiting.
#[derive(Debug, Clone)]
pub struct RateLimitConfig {
    /// Maximum requests per second (default: 50)
    pub requests_per_second: u32,
}

impl Default for RateLimitConfig {
    fn default() -> Self {
        Self {
            requests_per_second: 50,
        }
    }
}

impl RateLimitConfig {
    /// Load configuration from environment variables.
    pub fn from_env() -> Self {
        let requests_per_second = std::env::var("BASILICA_OPERATOR_K8S_RATE_LIMIT")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(50);

        Self {
            requests_per_second,
        }
    }
}

/// Configuration for probe timeouts.
#[derive(Debug, Clone)]
pub struct ProbeConfig {
    /// GPU workload startup probe initial delay (default: 10s)
    pub gpu_startup_initial_delay: i32,
    /// GPU workload startup probe period (default: 10s)
    pub gpu_startup_period: i32,
    /// GPU workload startup probe timeout (default: 5s)
    pub gpu_startup_timeout: i32,
    /// GPU workload startup probe failure threshold (default: 60)
    pub gpu_startup_failure_threshold: i32,
    /// Standard workload initial delay (default: 5s)
    pub std_initial_delay: i32,
    /// Standard workload period (default: 5s)
    pub std_period: i32,
    /// Standard workload timeout (default: 3s)
    pub std_timeout: i32,
    /// Standard workload failure threshold (default: 3)
    pub std_failure_threshold: i32,
}

impl Default for ProbeConfig {
    fn default() -> Self {
        Self {
            gpu_startup_initial_delay: 10,
            gpu_startup_period: 10,
            gpu_startup_timeout: 5,
            gpu_startup_failure_threshold: 60,
            std_initial_delay: 5,
            std_period: 5,
            std_timeout: 3,
            std_failure_threshold: 3,
        }
    }
}

impl ProbeConfig {
    /// Load configuration from environment variables.
    pub fn from_env() -> Self {
        Self {
            gpu_startup_initial_delay: parse_i32_env(
                "BASILICA_OPERATOR_GPU_STARTUP_INITIAL_DELAY_SECS",
                10,
            ),
            gpu_startup_period: parse_i32_env("BASILICA_OPERATOR_GPU_STARTUP_PERIOD_SECS", 10),
            gpu_startup_timeout: parse_i32_env("BASILICA_OPERATOR_GPU_STARTUP_TIMEOUT_SECS", 5),
            gpu_startup_failure_threshold: parse_i32_env(
                "BASILICA_OPERATOR_GPU_STARTUP_FAILURE_THRESHOLD",
                60,
            ),
            std_initial_delay: parse_i32_env("BASILICA_OPERATOR_STD_INITIAL_DELAY_SECS", 5),
            std_period: parse_i32_env("BASILICA_OPERATOR_STD_PERIOD_SECS", 5),
            std_timeout: parse_i32_env("BASILICA_OPERATOR_STD_TIMEOUT_SECS", 3),
            std_failure_threshold: parse_i32_env("BASILICA_OPERATOR_STD_FAILURE_THRESHOLD", 3),
        }
    }
}

/// Configuration for miscellaneous timeouts.
#[derive(Debug, Clone)]
pub struct TimeoutConfig {
    /// FUSE mount readiness timeout (default: 60s)
    pub fuse_mount_timeout_secs: i32,
    /// Pod eviction grace period (default: 30s)
    pub pod_eviction_grace_secs: i64,
    /// Cloud metadata endpoint timeout (default: 500ms)
    pub metadata_endpoint_timeout_ms: u64,
}

impl Default for TimeoutConfig {
    fn default() -> Self {
        Self {
            fuse_mount_timeout_secs: 60,
            pod_eviction_grace_secs: 30,
            metadata_endpoint_timeout_ms: 500,
        }
    }
}

impl TimeoutConfig {
    /// Load configuration from environment variables.
    pub fn from_env() -> Self {
        Self {
            fuse_mount_timeout_secs: parse_i32_env("BASILICA_OPERATOR_FUSE_MOUNT_TIMEOUT_SECS", 60),
            pod_eviction_grace_secs: parse_i64_env("BASILICA_OPERATOR_POD_EVICTION_GRACE_SECS", 30),
            metadata_endpoint_timeout_ms: parse_u64_env(
                "BASILICA_OPERATOR_METADATA_TIMEOUT_MS",
                500,
            ),
        }
    }
}

/// Complete operator configuration.
#[derive(Debug, Clone, Default)]
pub struct OperatorConfig {
    pub reconcile: ReconcileConfig,
    pub rate_limit: RateLimitConfig,
    pub probes: ProbeConfig,
    pub timeouts: TimeoutConfig,
}

impl OperatorConfig {
    /// Load complete configuration from environment variables.
    pub fn from_env() -> Self {
        Self {
            reconcile: ReconcileConfig::from_env(),
            rate_limit: RateLimitConfig::from_env(),
            probes: ProbeConfig::from_env(),
            timeouts: TimeoutConfig::from_env(),
        }
    }
}

/// Parse a duration from an environment variable.
fn parse_duration_env(var: &str, default_secs: u64) -> Duration {
    std::env::var(var)
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .map(Duration::from_secs)
        .unwrap_or_else(|| Duration::from_secs(default_secs))
}

/// Parse an i32 from an environment variable.
fn parse_i32_env(var: &str, default: i32) -> i32 {
    std::env::var(var)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

/// Parse an i64 from an environment variable.
fn parse_i64_env(var: &str, default: i64) -> i64 {
    std::env::var(var)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

/// Parse a u64 from an environment variable.
fn parse_u64_env(var: &str, default: u64) -> u64 {
    std::env::var(var)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_reconcile_config_defaults() {
        let config = ReconcileConfig::default();
        assert_eq!(config.success_interval, Duration::from_secs(30));
        assert_eq!(config.error_interval, Duration::from_secs(10));
        assert_eq!(config.node_profile_interval, Duration::from_secs(60));
    }

    #[test]
    fn test_rate_limit_config_defaults() {
        let config = RateLimitConfig::default();
        assert_eq!(config.requests_per_second, 50);
    }

    #[test]
    fn test_probe_config_defaults() {
        let config = ProbeConfig::default();
        assert_eq!(config.gpu_startup_initial_delay, 10);
        assert_eq!(config.gpu_startup_period, 10);
        assert_eq!(config.gpu_startup_timeout, 5);
        assert_eq!(config.gpu_startup_failure_threshold, 60);
        assert_eq!(config.std_initial_delay, 5);
        assert_eq!(config.std_period, 5);
        assert_eq!(config.std_timeout, 3);
        assert_eq!(config.std_failure_threshold, 3);
    }

    #[test]
    fn test_timeout_config_defaults() {
        let config = TimeoutConfig::default();
        assert_eq!(config.fuse_mount_timeout_secs, 60);
        assert_eq!(config.pod_eviction_grace_secs, 30);
        assert_eq!(config.metadata_endpoint_timeout_ms, 500);
    }

    #[test]
    fn test_operator_config_from_env_uses_defaults() {
        let config = OperatorConfig::from_env();
        assert_eq!(config.reconcile.success_interval, Duration::from_secs(30));
        assert_eq!(config.rate_limit.requests_per_second, 50);
    }
}
