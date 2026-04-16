//! # Cathedral CLI
//!
//! Unified command-line interface for Cathedral GPU rental and network management.
//!
//! This crate provides a comprehensive CLI that combines:
//! - GPU rental operations (ls, up, down, exec, ssh, etc.)
//! - Network component management (validator, miner, node)
//! - Configuration and wallet management
//!
//! ## Architecture
//!
//! The CLI follows the same patterns as other Cathedral components:
//! - Clap-based argument parsing with derive macros
//! - Handler-based command processing
//! - Shared configuration and error handling
//! - Integration with existing cathedral-common utilities

pub mod auth;
pub mod cli;
pub mod client;
pub mod config;
pub mod error;
pub mod github_releases;
pub mod interactive;
pub mod output;
pub mod progress;
pub mod source;
pub mod ssh;
pub mod types;
pub mod update_check;

pub use cli::*;
pub use error::*;
