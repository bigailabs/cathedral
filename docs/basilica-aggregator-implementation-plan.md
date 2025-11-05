# Basilica GPU Price Aggregator - Implementation Plan

## Project Overview

Create a new Rust crate (`basilica-aggregator`) that aggregates GPU pricing data from multiple cloud providers (DataCrunch, Hyperstack, Lambda Labs) and exposes a unified HTTP API for querying GPU offerings with pricing and specifications.

## Core Requirements

### Service Type
- Standalone HTTP service (not gRPC)
- New binary crate: `crates/basilica-aggregator/`
- Follows existing Basilica patterns (TOML config, SQLite, async Tokio, tracing logs)
- Should integrate into existing `just` command infrastructure

### Data Flow Architecture
```
Provider APIs → Fetcher (with cooldown) → SQLite (cache + historical) → HTTP API
                                              ↓
                                     Provider-specific modules
                                     (DataCrunch, Hyperstack, Lambda)
```

### Provider Module Architecture
- **Each provider in separate module**: `providers/datacrunch.rs`, `providers/hyperstack.rs`, `providers/lambda.rs`
- **Provider-specific types**: Each module defines its own API request/response types
- **Trait-based abstraction**: Common `Provider` trait for unified interface
- **Future-proof design**: Prepared for future GPU rental API integration (not just pricing)
- **MVP Focus**: Only implement instance fetching and pricing for DataCrunch in Phase 1

### Caching Strategy
- **SQLite as cache**: Use database as both cache and historical storage (no in-memory cache needed)
- **On-demand fetching**: First request triggers fetch, subsequent requests read from database until TTL expires
- **Per-provider cooldown timers**: Prevent excessive API calls (30 seconds minimum between fetches per provider)
- **TTL via fetched_at timestamp**: Check `fetched_at` column to determine if data is stale (45 seconds default)
- **Fallback behavior**: Return last successfully fetched data if provider is unavailable
- **Rationale**: Provider pricing changes infrequently (hours to days), and SQLite is fast enough for reads (<100ms target)

### Data Model - Strict Schema with Mapping Rules

**Core GpuOffering Structure:**
```
- id: String (provider-specific ID)
- provider: Provider enum (DataCrunch | Hyperstack | Lambda)
- gpu_type: GpuType enum (H100_80GB | A100_40GB | A100_80GB | V100_16GB | etc.)
- gpu_count: u32
- memory_gb: u32 (system RAM)
- vcpu_count: u32
- region: String (normalized region codes)
- hourly_rate: Decimal ($/hour on-demand pricing)
- spot_rate: Option<Decimal> ($/hour spot pricing if available)
- availability: bool
- fetched_at: DateTime<Utc>
- raw_metadata: JsonValue (stored in SQLite, NOT exposed in API)
```

**Important Data Model Notes:**
- Use `rust_decimal::Decimal` for precise price handling (never use floats for money)
- Normalize GPU types to canonical names (e.g., "H100-80GB", "A100-40GB")
- Drop offerings that cannot be mapped to canonical GPU types
- Region codes should be normalized (providers use different formats)
- Store complete provider response in `raw_metadata` for debugging/future use

### API Endpoints

**Primary Endpoint:**
- `GET /gpu-prices` - Query all GPU offerings with filters
  - Query parameters:
    - `gpu_type` (optional): Filter by GPU type (e.g., "H100_80GB")
    - `region` (optional): Filter by region
    - `provider` (optional): Filter by provider name
    - `min_price` (optional): Minimum hourly rate filter
    - `max_price` (optional): Maximum hourly rate filter
    - `available_only` (optional): Show only available offerings (default: false)
    - `sort_by` (optional): Sort field (price, gpu_type, region)
  - Returns: JSON array of GpuOffering objects (WITHOUT raw_metadata)

**Supporting Endpoints:**
- `GET /health` - Service health check (returns provider status)
- `GET /providers` - List enabled providers and their last fetch status

**Response Format:**
- Always return valid data (empty array if no results)
- Include metadata: `total_count`, `cached_at`, `provider_statuses`
- Never expose `raw_metadata` field in API responses

### Configuration Management

**Configuration File: `config/aggregator.toml.example`**
```
[server]
host = "0.0.0.0"
port = 8080

[cache]
ttl_seconds = 45

[providers.datacrunch]
enabled = true
api_key = "${DATACRUNCH_API_KEY}"  # env var override
cooldown_seconds = 30
timeout_seconds = 10
api_base_url = "https://api.datacrunch.io/v1"

[providers.hyperstack]
enabled = false  # not implemented in MVP
api_key = "${HYPERSTACK_API_KEY}"
cooldown_seconds = 30
timeout_seconds = 10
api_base_url = "https://infrahub-api.nexgencloud.com/v1"

[providers.lambda]
enabled = false  # not implemented in MVP
api_key = "${LAMBDA_API_KEY}"
cooldown_seconds = 30
timeout_seconds = 10
api_base_url = "https://cloud.lambda.ai/api/v1"

[database]
path = "aggregator.db"
```

**Configuration Priority:**
1. Environment variables (highest priority)
2. TOML configuration file
3. Hardcoded defaults (fallback)

**Environment Variables:**
- `DATACRUNCH_API_KEY` - Required for DataCrunch provider
- `HYPERSTACK_API_KEY` - Required for Hyperstack provider
- `LAMBDA_API_KEY` - Required for Lambda provider
- `AGGREGATOR_PORT` - Override server port
- `AGGREGATOR_DB_PATH` - Override database path

### Database Schema (SQLite)

**Table: `gpu_offerings`**
```sql
CREATE TABLE gpu_offerings (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    gpu_type TEXT NOT NULL,
    gpu_count INTEGER NOT NULL,
    memory_gb INTEGER NOT NULL,
    vcpu_count INTEGER NOT NULL,
    region TEXT NOT NULL,
    hourly_rate TEXT NOT NULL,  -- stored as string to preserve decimal precision
    spot_rate TEXT,              -- nullable, stored as string
    availability BOOLEAN NOT NULL,
    raw_metadata TEXT NOT NULL,  -- JSON blob of provider's raw response
    fetched_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_provider_gpu ON gpu_offerings(provider, gpu_type);
CREATE INDEX idx_fetched_at ON gpu_offerings(fetched_at);
CREATE INDEX idx_region ON gpu_offerings(region);
CREATE INDEX idx_availability ON gpu_offerings(availability);
```

**Table: `provider_status`**
```sql
CREATE TABLE provider_status (
    provider TEXT PRIMARY KEY,
    last_fetch_at TIMESTAMP,
    last_success_at TIMESTAMP,
    last_error TEXT,
    is_healthy BOOLEAN NOT NULL DEFAULT 1,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Database Notes:**
- Use `sqlx` with compile-time query verification and migrations
- Store prices as TEXT to preserve decimal precision (convert to Decimal in Rust)
- `raw_metadata` contains full provider response as JSON
- `created_at` tracks when record was first inserted
- `fetched_at` tracks when data was last fetched from provider
- `provider_status` table tracks cooldown timing and health for each provider

### MVP Scope (Phase 1) - DataCrunch Only

**Must Implement:**
1. ✅ DataCrunch provider integration only
2. ✅ HTTP API with query-based filtering (all query params listed above)
3. ✅ SQLite historical tracking with schema above
4. ✅ TOML configuration with environment variable overrides
5. ✅ Per-provider cooldown timer (30 seconds)
6. ✅ TTL-based caching (45 seconds default)
7. ✅ Strict GPU type normalization
8. ✅ Error handling: return last good data on provider failure
9. ✅ Health check endpoint showing provider status
10. ✅ Integration with `just` commands (build, test, run)

**Explicitly Out of Scope for Phase 1:**
- ❌ Hyperstack integration (Phase 2)
- ❌ Lambda Labs integration (Phase 2)
- ❌ Price trend analysis endpoints
- ❌ Webhooks/notifications for price changes
- ❌ Prometheus metrics
- ❌ Authentication/authorization
- ❌ Rate limiting on API endpoints
- ❌ Docker deployment configuration

## Provider API Details

### DataCrunch API (Phase 1 - Implement This)

**Base URL:** `https://api.datacrunch.io/v1`

**Authentication:** Bearer token in Authorization header
```
Authorization: Bearer {DATACRUNCH_API_KEY}
```

**Key Endpoints:**

1. **GET /v1/instance-types**
   - Returns list of available GPU instance types
   - Supports optional `currency` query parameter (usd/eur) - use "usd"
   - Response fields per instance type:
     - `id` (string)
     - `instance_type` (string, e.g., "8V100.48M")
     - `price_per_hour` (float)
     - `spot_price_per_hour` (float)
     - `description` (string)
     - `cpu` (dict with CPU details)
     - `gpu` (dict with GPU details)
     - `memory` (dict with memory details)
     - `gpu_memory` (dict with GPU memory details)
     - `storage` (dict with storage details)

2. **GET /v1/instance-availability**
   - Check availability across all locations
   - Optional query params: `is_spot`, `location_code`
   - Returns availability status per instance type

3. **GET /v1/locations**
   - List available data centers
   - Returns: `code`, `name`, `country_code`

**DataCrunch-Specific Notes:**
- Offers both **fixed** and **dynamic** pricing (dynamic changes daily)
- Provides historical price data via `/v1/instance-types/price-history`
- Has spot pricing available (`spot_price_per_hour`)
- Response includes nested dicts for specs - store entire response in `raw_metadata`

**Official Documentation:**
- API Docs: https://api.datacrunch.io/v1/docs
- Python SDK (reference): https://github.com/DataCrunch-io/datacrunch-python

### Hyperstack API (Phase 2 - Future)

**Base URL:** `https://infrahub-api.nexgencloud.com/v1`

**Authentication:** API key in header
```
api_key: {HYPERSTACK_API_KEY}
```

**Key Endpoints:**
- Pricebook API: `GET /v1/pricebook/calculate/resource/{resource_type}/{resource_id}`
- Requires resource ID - may need two-step process to discover resources first

**Hyperstack-Specific Notes:**
- Pricing calculated hourly
- Supports discounts
- May require listing resources before fetching pricing
- Less documentation available than DataCrunch

**Official Documentation:**
- API Docs: https://portal.hyperstack.cloud/knowledge/api-documentation
- Pricebook: https://docs.hyperstack.cloud/docs/billing/pricebook/

### Lambda Labs API (Phase 2 - Future)

**Base URL:** `https://cloud.lambda.ai/api/v1`

**Authentication:** HTTP Basic Auth with API key
```python
# Example
auth=HTTPBasicAuth(API_KEY, "")
```

**Key Endpoints:**
- `GET /instance-types` - List available instance types with pricing

**Lambda-Specific Notes:**
- Pricing: H100 from $2.49/hr, B200 from $2.99/hr, A100 from $1.10/hr
- Billed by the minute
- No spot pricing mentioned
- Appears to have stable fixed pricing
- Less granular API documentation available publicly

**Official Documentation:**
- API Docs: https://docs.lambda.ai/api/cloud
- Support: https://support.lambdalabs.com/

## Module Structure

### Provider Module Organization

```
crates/basilica-aggregator/
├── src/
│   ├── main.rs                    # Binary entry point
│   ├── lib.rs                      # Library exports
│   ├── config.rs                   # Configuration management
│   ├── models.rs                   # Shared data models (GpuOffering, Provider enum, GpuType enum)
│   ├── db.rs                       # Database layer with sqlx
│   ├── api/
│   │   ├── mod.rs                  # HTTP API setup
│   │   ├── handlers.rs             # Route handlers
│   │   └── query.rs                # Query parameter types
│   ├── providers/
│   │   ├── mod.rs                  # Provider trait definition
│   │   ├── datacrunch/
│   │   │   ├── mod.rs              # DataCrunch provider implementation
│   │   │   ├── types.rs            # DataCrunch-specific API types
│   │   │   ├── client.rs           # HTTP client for DataCrunch API
│   │   │   └── normalize.rs        # GPU type normalization for DataCrunch
│   │   ├── hyperstack/             # Phase 2
│   │   │   └── mod.rs              # Placeholder
│   │   └── lambda/                 # Phase 2
│   │       └── mod.rs              # Placeholder
│   ├── service.rs                  # Core service logic (fetching, caching, cooldown)
│   └── error.rs                    # Error types
├── migrations/
│   └── 001_init.sql                # Database schema
└── Cargo.toml
```

### Provider Trait Design

```rust
#[async_trait]
pub trait Provider: Send + Sync {
    /// Unique provider identifier
    fn provider_id(&self) -> ProviderEnum;

    /// Fetch GPU offerings from provider API
    async fn fetch_offerings(&self) -> Result<Vec<GpuOffering>, ProviderError>;

    /// Health check for provider API
    async fn health_check(&self) -> Result<ProviderHealth, ProviderError>;

    // Future methods for Phase 2+ (not implemented in MVP):
    // async fn rent_instance(&self, instance_id: &str) -> Result<RentalInfo, ProviderError>;
    // async fn list_active_rentals(&self) -> Result<Vec<RentalInfo>, ProviderError>;
}
```

**Key Design Principles:**
- Each provider module is self-contained with its own types
- Provider trait provides unified interface for service layer
- DataCrunch types in `providers/datacrunch/types.rs` match actual API response structure
- Normalization happens in provider-specific modules (e.g., `datacrunch/normalize.rs`)
- Service layer works with normalized `GpuOffering` type from `models.rs`

## Technical Stack

### Core Dependencies
- **HTTP Framework:** `axum` (consistent with basilica-api)
- **HTTP Client:** `reqwest` with TLS support
- **Async Runtime:** `tokio` (already used in Basilica)
- **Database:** `sqlx` with SQLite runtime
- **Serialization:** `serde`, `serde_json`
- **Decimal Precision:** `rust_decimal` for price handling
- **Configuration:** `config` crate with TOML support
- **Logging:** `tracing` and `tracing-subscriber`
- **Time:** `chrono` for timestamps
- **Error Handling:** `anyhow` for application errors, `thiserror` for library errors

### Caching Implementation
- **SQLite-based caching**: No in-memory cache needed (`Arc<RwLock<>>` not required)
- Query database to check if data is fresh (compare `fetched_at` + TTL vs current time)
- If stale and cooldown period elapsed: fetch from provider and update database
- If stale but within cooldown: return existing database data
- Database queries are fast enough (<100ms target) for this use case

### Rate Limiting (Per-Provider Cooldown)
- Track last fetch time per provider in a separate `provider_status` table or in-memory map
- Enforce minimum cooldown period (configurable, default 30s)
- Use `tokio::time::Instant` for timing or compare timestamps
- Return cached data from database if within cooldown period

## Error Handling Strategy

### Provider Fetch Failures
- Log error with structured logging (tracing)
- Return last successfully cached data if available
- Include error status in health endpoint
- Never fail entire request if one provider fails

### Database Errors
- Log and continue serving from cache
- Historical tracking is best-effort, not critical path
- API should work even if DB writes fail

### Configuration Errors
- Fail fast on startup if config is invalid
- Required fields: at least one provider enabled with API key
- Validate URLs, timeouts, and numeric ranges

### API Request Errors
- Return proper HTTP status codes:
  - 200 OK: Success with data
  - 400 Bad Request: Invalid query parameters
  - 500 Internal Server Error: Unexpected failures
  - 503 Service Unavailable: All providers down and no cache

## GPU Type Normalization Rules

### Canonical GPU Types (GpuType enum)
Create mappings for common GPU types:
- H100_80GB (NVIDIA H100 80GB)
- H100_94GB (NVIDIA H100 NVL 94GB)
- A100_40GB (NVIDIA A100 40GB)
- A100_80GB (NVIDIA A100 80GB SXM/PCIe)
- V100_16GB (NVIDIA V100 16GB)
- V100_32GB (NVIDIA V100 32GB)
- A10_24GB (NVIDIA A10 24GB)
- A6000_48GB (NVIDIA RTX A6000 48GB)
- L40_48GB (NVIDIA L40 48GB)
- B200 (NVIDIA B200 - check actual memory size)
- GH200 (NVIDIA GH200 Superchip - check specs)

### Normalization Strategy
1. Parse provider's GPU description/type field
2. Extract GPU model and memory size
3. Map to canonical GpuType enum
4. If mapping fails, log warning and DROP the offering (strict schema requirement)
5. Store original GPU description in `raw_metadata`

### Region Normalization
- Create mapping of provider-specific region codes to normalized format
- Example: "us-east-1" (AWS-style) → "us-east"
- Store original region code in `raw_metadata`

## Testing Requirements

### Unit Tests
- Provider response parsing and normalization
- Cache TTL and cooldown logic
- Configuration loading with env overrides
- GPU type mapping functions
- Price decimal handling

### Integration Tests
- Full API endpoint testing with mock provider responses
- Database read/write operations
- Cache hit/miss scenarios
- Error handling when providers fail
- Configuration validation

### Manual Testing Checklist
1. Start service with valid DataCrunch API key
2. Call `/gpu-prices` - verify data returned
3. Call again within TTL - verify cache hit
4. Wait for TTL expiry - verify fresh fetch
5. Test all query filters (gpu_type, region, price range, etc.)
6. Test `/health` endpoint
7. Test `/providers` endpoint
8. Verify SQLite database populated with historical data
9. Kill provider API access - verify fallback to cached data
10. Check logs for proper structured logging

## Deployment Considerations

### Just Commands to Add
Add to root `justfile`:
```
# Run aggregator service
aggregator:
    cargo run --bin basilica-aggregator

# Build aggregator
build-aggregator:
    cargo build --release -p basilica-aggregator

# Test aggregator
test-aggregator:
    cargo test -p basilica-aggregator
```

### Configuration Setup
1. Copy `config/aggregator.toml.example` to `config/aggregator.toml`
2. Set environment variable: `export DATACRUNCH_API_KEY=your_key_here`
3. Run: `just aggregator`

### Database Location
- Development: `./aggregator.db` (gitignored)
- Production: Configurable via TOML or env var

## Performance Targets

- API response time: < 100ms (cache hit)
- Provider fetch timeout: 10 seconds max
- Cache memory usage: < 10MB for typical dataset
- Database write: async, non-blocking to API requests
- Support 100+ concurrent requests

## Logging and Observability

### Structured Logging Events
- Provider fetch attempts (start, success, failure)
- Cache hits/misses
- Database operations
- API requests (endpoint, query params, response time)
- Configuration loaded
- Errors with full context

### Log Levels
- DEBUG: Cache operations, detailed request info
- INFO: Provider fetches, API requests, startup
- WARN: Provider failures, normalization failures
- ERROR: Database errors, unexpected failures

### Metrics to Log
- Provider fetch duration
- API response time
- Cache hit rate
- Number of offerings per provider
- Database write latency

## Security Considerations

### API Key Management
- NEVER log API keys
- Load from environment variables or encrypted config
- Validate API keys on startup
- Use TLS for all provider API calls

### Input Validation
- Sanitize all query parameters
- Validate numeric ranges (prices, counts)
- Limit query result sizes to prevent DOS

### SQLite Security
- Database file permissions (600)
- No SQL injection risk (using sqlx parameter binding)
- Regular backups recommended

## Gotchas and Edge Cases

### Provider API Quirks
1. **DataCrunch:** Returns nested dicts for specs - need to parse carefully
2. **Currency Handling:** Always request USD, but be prepared for EUR responses
3. **Spot vs Fixed:** DataCrunch has both - prioritize on-demand pricing in primary field
4. **GPU Names:** Inconsistent naming across providers (e.g., "A100 SXM4" vs "A100-80GB")

### Caching Edge Cases
1. Cold start: No cache data - first request triggers fetch
2. All providers down + no cache: Return 503 with helpful error
3. Partial provider failure: Return data from working providers
4. Stale cache after restart: Timestamp in DB helps determine freshness

### Data Quality
1. Missing fields in provider response: Use Option<T> and handle gracefully
2. Invalid pricing (negative, zero): Log and skip offering
3. Unknown GPU types: Log and skip (strict schema)
4. Duplicate offerings: Use provider + instance_id as unique key

### Timezone Handling
- Always use UTC for timestamps
- Store `fetched_at` in UTC
- Return timestamps in ISO 8601 format

## Future Enhancements (Not in MVP)

### Phase 2
- Hyperstack provider integration
- Lambda Labs provider integration
- Comprehensive region normalization
- API filtering enhancements

### Phase 3
- Price trend analysis endpoints (`GET /gpu-prices/trends`)
- Historical price charts
- Price drop alerts
- Webhooks for price changes

### Phase 4
- Prometheus metrics endpoint
- Grafana dashboard
- Docker deployment with docker-compose
- Kubernetes manifests
- API authentication (API keys)
- Rate limiting on endpoints

## Success Criteria

MVP is complete when:
1. ✅ DataCrunch provider fully integrated and tested
2. ✅ All API endpoints working with proper filtering
3. ✅ SQLite historical data being stored
4. ✅ Caching with TTL working correctly
5. ✅ Cooldown timers preventing excessive API calls
6. ✅ Configuration loading from TOML + env vars
7. ✅ Error handling gracefully falling back to cached data
8. ✅ Health and provider status endpoints working
9. ✅ Integration tests passing
10. ✅ Documentation in README with setup instructions
11. ✅ `just` commands integrated
12. ✅ Can run `just aggregator` and query live data

## Reference Documentation

- DataCrunch API: https://api.datacrunch.io/v1/docs
- Basilica Architecture: See CLAUDE.md in repo root
- Axum Framework: https://docs.rs/axum
- SQLx: https://docs.rs/sqlx
- Rust Decimal: https://docs.rs/rust_decimal

## Questions for Implementation

1. **GPU Memory Extraction:** How to reliably parse GPU memory from provider descriptions?
   - Suggestion: Regex patterns per provider + manual mapping table

2. **Region Normalization:** Should we maintain a static mapping file or dynamic discovery?
   - Suggestion: Start with static mapping, make extensible

3. **Cache Invalidation:** Should we support manual cache clearing endpoint?
   - Suggestion: Add `/admin/cache/clear` endpoint for debugging

4. **Price History:** Should we implement price change tracking in MVP?
   - Decision: No, Phase 2 feature. Just store snapshots in DB for now.

5. **Multiple Instance Types:** How to handle providers offering same GPU with different configs?
   - Suggestion: Treat each unique configuration as separate offering with unique ID
