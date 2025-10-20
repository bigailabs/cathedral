//! Common test utilities for basilica-billing tests
//!
//! This module provides automatic Docker-based PostgreSQL setup for tests.

pub mod test_db;

pub use test_db::{get_test_database_url, get_test_pool, init_test_database};
