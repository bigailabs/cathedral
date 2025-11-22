//! Basilica GPU Price Aggregator
//!
//! Aggregates GPU pricing data from multiple cloud providers.

pub mod background;
pub mod config;
pub mod db;
pub mod error;
pub mod models;
pub mod providers;
pub mod service;

// Re-export commonly used types for easy access
pub use config::Config as AggregatorConfig;
pub use db::Database;
pub use error::{AggregatorError, Result};
pub use models::{
    Deployment, DeploymentStatus, GpuOffering, Provider as ProviderEnum, ProviderHealth, SshKey,
};
pub use service::AggregatorService;
