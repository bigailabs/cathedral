# Changelog

All notable changes to the Basilica Python SDK will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.0] - 2025-12-09

### Added
- Progress callback support in `wait_until_ready()` with `on_progress` and `silent` parameters
- `wait_for_ready()` function with progress callback support in Rust SDK
- Deployment events, scaling, and health check types

### Changed
- Improved `wait_for_ready` state tracking and ready condition logic

## [0.8.0] - 2025-12-06

### Added
- Deployment progress and resource request bindings

## [0.7.0] - 2025-12-05

### Added
- Deployment progress tracking with `ProgressInfo` dataclass
- Progress callbacks in `wait_until_ready()` via `on_progress` parameter
- `DeploymentStatus.progress` field for tracking sync and startup progress

### Changed
- Improved deployment status reporting with detailed phase information

## [0.6.0] - 2025-11-13

### Added
- `@deployment` decorator for declarative function-based deployments
- `DeployedFunction` wrapper class for decorator API
- `DeploymentSpec` frozen dataclass for immutable deployment configuration
- High-level `deploy()` method on `BasilicaClient` for one-line deployments
- `Deployment` facade class with `url`, `logs()`, `delete()`, `status()` methods
- `DeploymentStatus` dataclass with `is_ready`, `is_failed`, `is_pending` properties
- Core facade modules: `deployment.py`, `decorators.py`, `spec.py`
- `Volume` class for persistent storage with `from_name()` factory method
- `SourcePackager` class for automatic source code packaging
- Framework auto-detection (FastAPI, Flask, Django) in `SourcePackager`
- GPU requirements support: `gpu_count`, `gpu_models`, `min_cuda_version`, `min_gpu_memory_gb`
- Storage support: `storage=True` or `storage="/path"` parameter

### Changed
- `BasilicaClient` now exposes both high-level (`deploy()`, `get()`, `list()`) and low-level APIs
- Bucket parameter is now optional in `PersistentStorageSpec`

## [0.5.0] - 2025-11-11

### Added
- Python SDK bindings for deployment operations via PyO3
- `create_deployment()`, `get_deployment()`, `delete_deployment()`, `list_deployments()` methods
- `get_deployment_logs()` for streaming container logs
- Deployment response types: `DeploymentResponse`, `DeploymentSummary`, `DeploymentListResponse`

## [0.4.0] - 2025-11-11

### Added
- GPU and storage types exposed to Python SDK
- `GpuRequirementsSpec` for GPU resource specifications
- `StorageSpec`, `PersistentStorageSpec`, `StorageBackend` types
- `ResourceRequirements` with GPU support

## [0.3.0] - 2025-11-11

### Added
- Public deployment parameter for creating public URLs
- Improved delete response serialization

### Fixed
- Environment variable serialization type handling

## [0.2.0] - 2025-11-11

### Added
- Comprehensive exception hierarchy with 12 exception types
- `BasilicaError`, `AuthenticationError`, `AuthorizationError`, `ValidationError`
- `DeploymentError`, `DeploymentNotFound`, `DeploymentTimeout`, `DeploymentFailed`
- `ResourceError`, `StorageError`, `NetworkError`, `RateLimitError`, `SourceError`

### Changed
- Migrated from JWT tokens to API key authentication
- Standardized API token environment variable naming to `BASILICA_API_TOKEN`

### Fixed
- PyO3 signature default value syntax for v0.26 compatibility
- pyo3-stub-gen build errors with extension-module feature

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
- Type hints via `.pyi` stub files for IDE support
- PyO3-based Rust bindings for performance
- Cross-platform support (Linux, macOS, Windows)

### Documentation
- README with installation and usage instructions
- Inline API documentation
- Example code for common workflows

[Unreleased]: https://github.com/one-covenant/basilica/compare/basilica-sdk-python-v0.9.0...HEAD
[0.9.0]: https://github.com/one-covenant/basilica/compare/basilica-sdk-python-v0.8.0...basilica-sdk-python-v0.9.0
[0.8.0]: https://github.com/one-covenant/basilica/compare/basilica-sdk-python-v0.7.0...basilica-sdk-python-v0.8.0
[0.7.0]: https://github.com/one-covenant/basilica/compare/basilica-sdk-python-v0.6.0...basilica-sdk-python-v0.7.0
[0.6.0]: https://github.com/one-covenant/basilica/compare/basilica-sdk-python-v0.5.0...basilica-sdk-python-v0.6.0
[0.5.0]: https://github.com/one-covenant/basilica/compare/basilica-sdk-python-v0.4.0...basilica-sdk-python-v0.5.0
[0.4.0]: https://github.com/one-covenant/basilica/compare/basilica-sdk-python-v0.3.0...basilica-sdk-python-v0.4.0
[0.3.0]: https://github.com/one-covenant/basilica/compare/basilica-sdk-python-v0.2.0...basilica-sdk-python-v0.3.0
[0.2.0]: https://github.com/one-covenant/basilica/compare/basilica-sdk-python-v0.1.0...basilica-sdk-python-v0.2.0
[0.1.0]: https://github.com/one-covenant/basilica/releases/tag/basilica-sdk-python-v0.1.0
