# Basilica Billing Service

The billing service handles GPU rental pricing, package management, and cost calculation for the Basilica network.

## Features

- Dynamic GPU pricing from marketplace aggregator
- Package-based billing with volume discounts
- Credit-based system
- gRPC API for rental management and price queries

## Dynamic GPU Pricing

### Marketplace Provider

The billing service uses dynamic GPU pricing through a marketplace aggregator API that provides real-time pricing from 10+ GPU cloud providers worldwide.

#### Configuration

Production configuration example:

```toml
[pricing]
enabled = true
global_discount_percent = -20.0
update_interval_seconds = 86400
cache_ttl_seconds = 86400
fallback_to_static = true
sources = ["marketplace"]
aggregation_strategy = "median"

# Marketplace API configuration
marketplace_api_key = "${MARKETPLACE_API_KEY}"  # From AWS Secrets Manager
marketplace_available_only = true
# marketplace_api_url = "https://api.shadeform.ai/v1"  # Optional, uses default
```

Development configuration example:

```toml
[pricing]
enabled = true
global_discount_percent = 0.0  # No discount for testing
update_interval_seconds = 3600  # Sync every hour
cache_ttl_seconds = 3600
fallback_to_static = true
sources = ["marketplace"]
aggregation_strategy = "median"

# Test API key (not in version control!)
marketplace_api_key = "${MARKETPLACE_API_KEY}"
marketplace_available_only = false  # Show all instances for testing
```

#### Environment Variables

- `MARKETPLACE_API_KEY`: API key for marketplace provider (required when `sources` contains `"marketplace"`)

#### Configuration Options

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `false` | Enable dynamic pricing |
| `global_discount_percent` | `Decimal` | `-20.0` | Global discount percentage (negative = discount) |
| `update_interval_seconds` | `u64` | `86400` | How often to fetch prices (seconds) |
| `cache_ttl_seconds` | `u64` | `86400` | Cache time-to-live (seconds) |
| `fallback_to_static` | `bool` | `true` | Fall back to static prices if API fails |
| `sources` | `Vec<PriceSource>` | `["marketplace"]` | Price sources to query |
| `aggregation_strategy` | `PriceAggregationStrategy` | `"average"` | How to aggregate prices from multiple sources |
| `marketplace_api_key` | `Option<String>` | `None` | Marketplace API key (required) |
| `marketplace_api_url` | `String` | `"https://api.shadeform.ai/v1"` | Marketplace API URL |
| `marketplace_available_only` | `bool` | `true` | Only return available instances |

#### Price Sources

- `marketplace`: GPU marketplace aggregator (Shadeform API) - default and recommended
- `custom`: Custom API endpoint (not yet implemented)

#### Aggregation Strategies

**All strategies normalize prices by GPU count before aggregation**, ensuring fair comparison between different configurations (1x, 2x, 4x, 8x GPUs). This means a 2x H100 at $4/hr is correctly recognized as $2 per GPU, not treated as a separate $4/hr option.

- `minimum`: Use lowest **per-GPU** price across all sources
- `average`: Use average **per-GPU** price (default)

**Example:**
- Provider A: 1x H100 @ $2/hr → $2 per GPU
- Provider B: 2x H100 @ $4/hr → $2 per GPU
- Provider C: 8x H100 @ $24/hr → $3 per GPU
- **Average strategy**: ($2 + $2 + $3) / 3 = **$2.33 per GPU** ✅
- **Without normalization**: ($2 + $4 + $24) / 3 = $10/hr ❌ (incorrect!)

#### How It Works

1. **Price Fetching**: The service fetches GPU prices from the marketplace API at configured intervals (default: daily at 2 AM UTC)
2. **Price Normalization**: All prices are normalized to per-GPU cost for fair comparison across multi-GPU configurations
3. **Caching**: Prices are cached in the database to reduce API calls and improve performance
4. **Discount Application**: Global and per-GPU discounts are applied to market prices
5. **Automatic Updates**: Prices are automatically refreshed based on the configured sync schedule
6. **Fallback**: If the API is unavailable, the service falls back to static prices (if enabled)

#### Monitored GPU Models

The billing service queries prices for GPU models that are officially supported by the Basilica validator network:
- **A100** - High-end training & inference
- **H100** - Flagship AI training & inference
- **B200** - Next-gen AI acceleration

These models are defined in `basilica-common::types::GpuCategory` and represent the GPUs that the validator network can score and allocate. You can configure custom GPU models through the API query filters if needed.

#### API Key Setup

**Development**:
```bash
export MARKETPLACE_API_KEY="your-api-key-here"
```

**Production** (AWS Secrets Manager):
```bash
# Store API key in AWS Secrets Manager
aws secretsmanager create-secret \
  --name basilica/marketplace-api-key \
  --secret-string "your-api-key-here"

# Reference in configuration
marketplace_api_key = "${MARKETPLACE_API_KEY}"
```

## Package System

The billing service supports GPU package tiers with volume discounts:

- **1 GPU**: Base price
- **2 GPUs**: 5% volume discount
- **4 GPUs**: 10% volume discount
- **8 GPUs**: 15% volume discount

Packages include:
- GPU compute hours
- Network egress allowance
- Storage allowance

Overages are billed separately at configured rates.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    PricingService                           │
│  - Fetches prices from marketplace                          │
│  - Applies discount strategies                              │
│  - Manages sync scheduling                                  │
│  - Caches prices in database                                │
└─────────────────────────────────────────────────────────────┘
                              │
                              │
                    ┌─────────▼──────────┐
                    │   PriceProvider    │
                    │       Trait        │
                    └────────────────────┘
                              │
                    ┌─────────▼──────────┐
                    │ MarketplaceProvider│
                    │                    │
                    │  Shadeform API     │
                    │  (Aggregator)      │
                    └────────────────────┘
                              │
                              │
                    ┌─────────▼──────────┐
                    │   10+ GPU Cloud    │
                    │     Providers      │
                    │                    │
                    │  - North America   │
                    │  - Europe          │
                    │  - Asia Pacific    │
                    │                    │
                    │  (Aggregated via   │
                    │   marketplace API) │
                    └────────────────────┘
```

### Data Flow

```
Background Sync (Daily at 2 AM UTC):
  1. MarketplaceProvider queries Shadeform API
  2. Receives prices from all available providers
  3. PricingService aggregates by GPU model
  4. Applies configured discounts
  5. Stores in price_cache table
  6. Sets 24-hour expiration

Rental Creation:
  1. User requests GPU rental
  2. PackageRepository loads package for GPU model
  3. If use_dynamic_pricing=true:
     - Queries price_cache for latest price
     - Falls back to static price if cache miss
  4. Creates rental with current market price
```

## Testing

Run all tests:
```bash
cargo test --lib
```

Run specific test suites:
```bash
# Marketplace provider tests
cargo test --lib pricing::providers::marketplace

# Pricing service tests
cargo test --lib pricing::service

# Configuration tests
cargo test --lib pricing::types
```

## Troubleshooting

### "Marketplace API key is required"
- Ensure `marketplace_api_key` is set in configuration or environment
- Verify the environment variable is properly loaded
- Check that the variable name matches exactly

### No prices returned
- Check marketplace API key is valid
- Verify network connectivity to marketplace API (`https://api.shadeform.ai/v1`)
- Check logs for API errors or rate limiting
- Ensure `fallback_to_static = true` if using fallback

### Stale prices
- Verify `update_interval_seconds` is set appropriately (default: 86400)
- Review cache TTL settings (`cache_ttl_seconds`)
- Check background sync job is running (look for sync logs)
- Manually trigger sync via gRPC: `SyncPrices` RPC

### Prices seem incorrect
- Check `global_discount_percent` configuration (default: -20%)
- Verify per-GPU discount overrides in `gpu_discounts`
- Check `aggregation_strategy` (minimum, median, or average)
- Review price history via gRPC: `GetPriceHistory` RPC

### High API usage
- Increase `cache_ttl_seconds` to reduce sync frequency
- Set `update_interval_seconds` higher (e.g., 86400 for daily)
- Enable `marketplace_available_only = true` to reduce query size
- Monitor API rate limits via response headers

## Monitoring

The pricing service exposes Prometheus metrics for monitoring:

- `pricing_sync_total`: Total number of price sync operations
- `pricing_sync_errors_total`: Failed sync operations
- `pricing_fetch_duration_seconds`: Time to fetch prices from marketplace
- `pricing_cache_size`: Number of cached price entries
- `pricing_cache_hits_total`: Cache hit count
- `pricing_cache_misses_total`: Cache miss count
- `pricing_fallback_to_static_total`: Fallback to static prices count

Monitor these to ensure pricing service health and performance.

## License

See LICENSE file in repository root.
