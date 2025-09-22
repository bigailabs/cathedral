//! Connection subsystem: pooling, health checks, connection state, retries, and monitors.
//!
//! This module groups all connection-related primitives behind a cohesive API while
//! re-exporting items to keep the public surface stable.

pub mod pool;
pub mod state;
pub mod health;
pub mod monitor;

// Re-export core types from submodules
pub use pool::{ConnectionPool, ConnectionPoolBuilder};
pub use state::{ConnectionManager, ConnectionMetricsSnapshot, ConnectionState};
pub use health::{HealthCheckMetrics, HealthChecker, ConnectionPoolTrait};
pub use monitor::{BlockchainMonitor, BlockchainEventHandler};
pub use crate::retry::{CircuitBreaker, ExponentialBackoff, RetryExecutor};
pub use crate::error::RetryConfig;

/// Common imports for connection-related code
pub mod prelude {
    pub use super::{
        BlockchainMonitor, CircuitBreaker, ConnectionManager, ConnectionMetricsSnapshot,
        ConnectionPool, ConnectionPoolBuilder, ConnectionPoolTrait, ConnectionState,
        ExponentialBackoff, HealthCheckMetrics, HealthChecker, RetryConfig, RetryExecutor,
    };
}
