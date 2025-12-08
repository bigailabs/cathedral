//! Source file handling for deploy command
//!
//! This module provides functionality for detecting, reading, and packaging
//! source files for deployment, including framework detection.

mod packager;

pub use packager::{Framework, SourcePackager, SourceType};
