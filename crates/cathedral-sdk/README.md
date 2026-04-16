# Cathedral SDK

Official Rust SDK for interacting with the Cathedral GPU rental network.

## Overview

This SDK provides a type-safe, async Rust client for the Cathedral API. It was extracted from the `cathedral-api` crate to enable code reuse across multiple consumers:

- **cathedral-api**: Re-exports the SDK for backward compatibility
- **cathedral-cli**: Uses the SDK directly for all API interactions
- **cathedral-sdk-python**: Python bindings built on top of this SDK

## Installation

Add to your `Cargo.toml`:

```toml
[dependencies]
cathedral-sdk = "0.1"
```

## Usage

For complete usage examples and API documentation, please refer to the examples in the codebase that demonstrate how to use the SDK for various tasks including:
- Creating and configuring clients
- Starting GPU rentals
- Managing resources
- Handling authentication
- Error handling patterns

## Features

- **Async/await support** - Built on tokio for async operations
- **Type safety** - Strongly typed request/response models
- **Error handling** - Comprehensive error types with retry hints
- **Authentication** - JWT Bearer token authentication
- **Configurable** - Timeouts, connection pooling, etc.

## Testing

Run tests with:

```bash
cargo test -p cathedral-sdk
```

## License

MIT OR Apache-2.0