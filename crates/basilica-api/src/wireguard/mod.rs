//! WireGuard configuration module for Basilica API
//!
//! This module provides WireGuard VPN configuration for remote GPU nodes
//! that join the K3s cluster over the internet. It includes:
//!
//! - Deterministic IP allocation from node_id hash
//! - Configuration types for API responses
//! - Multi-server peer configuration for HA routing
//! - Integration with the GPU node registration flow

mod config;
mod ip_allocator;

pub use config::WireGuardConfig;
pub use config::WireGuardPeer;
pub use config::WireGuardServerConfig;
pub use config::WireGuardServerEntry;
pub use ip_allocator::allocate_wireguard_ip;
pub use ip_allocator::is_valid_gpu_node_ip;
pub use ip_allocator::is_valid_wireguard_public_key;
