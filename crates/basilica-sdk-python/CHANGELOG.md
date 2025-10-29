# Changelog

All notable changes to the Basilica Python SDK will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2025-10-10

### Added
- Initial release of Basilica Python SDK
- Support for GPU rental management via Basilica API
- Client authentication via API keys (environment variable or direct)
- Health check functionality for API monitoring
- Node listing and filtering with query parameters
- Rental lifecycle management:
  - Start rentals with flexible node selection
  - Get rental status with SSH access information
  - Stop active rentals
  - List all rentals with optional filtering
- SSH access utilities for easy connection to rental instances
- Auto-configuration from environment variables:
  - `BASILICA_API_URL` for API endpoint
  - `BASILICA_API_TOKEN` for authentication
- Comprehensive examples demonstrating common use cases:
  - `quickstart.py` - Minimal getting started example
  - `start_rental.py` - Complete rental workflow
  - `list_nodes.py` - Finding available GPU resources
  - `health_check.py` - API health monitoring
  - `ssh_utils.py` - SSH credential handling
- Type hints via `.pyi` stub files for IDE support
- PyO3-based Rust bindings for performance
- Cross-platform support (Linux, macOS, Windows)

### Documentation
- README with installation and usage instructions
- Inline API documentation
- Example code for common workflows

[Unreleased]: https://github.com/one-covenant/basilica/compare/basilica-sdk-python-v0.1.0...HEAD
[0.1.0]: https://github.com/one-covenant/basilica/releases/tag/basilica-sdk-python-v0.1.0
