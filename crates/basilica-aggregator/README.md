# Basilica GPU Price Aggregator

A standalone HTTP service that aggregates GPU pricing data from multiple cloud providers.

## Features

- DataCrunch provider integration
- SQLite-based caching with TTL
- Per-provider cooldown timers
- REST API with query filtering
- Health check endpoints

## Quick Start

### Prerequisites

- Rust 1.80+
- DataCrunch OAuth2 credentials (client_id and client_secret)

### Setup

1. Copy example config:
```bash
cp config/aggregator.toml.example config/aggregator.toml
```

2. Edit `config/aggregator.toml` and add your DataCrunch OAuth2 credentials:
```toml
[providers.datacrunch]
enabled = true
client_id = "your-actual-client-id"
client_secret = "your-actual-client-secret"
```

3. Run the service:
```bash
just aggregator
```

The service will start on `http://localhost:8080`

## API Endpoints

### Get GPU Prices

```bash
GET /gpu-prices?gpu_type=H100_80GB&available_only=true&sort_by=price
```

Query parameters:
- `gpu_type` (optional): Filter by GPU type
- `region` (optional): Filter by region
- `provider` (optional): Filter by provider
- `min_price` (optional): Minimum hourly rate
- `max_price` (optional): Maximum hourly rate
- `available_only` (optional): Show only available offerings
- `sort_by` (optional): Sort by `price`, `gpu_type`, or `region`

### Health Check

```bash
GET /health
```

Returns provider health status.

### Provider Status

```bash
GET /providers
```

Returns enabled providers and their last fetch status.

## Configuration

Configuration is managed via `config/aggregator.toml`.

**Primary configuration (in TOML file):**
- Set OAuth2 credentials directly in the config file:
  ```toml
  [providers.datacrunch]
  client_id = "your-client-id"
  client_secret = "your-client-secret"
  ```

**Optional environment variable overrides:**
- `AGGREGATOR__PROVIDERS__DATACRUNCH__CLIENT_ID` - Override DataCrunch client ID
- `AGGREGATOR__PROVIDERS__DATACRUNCH__CLIENT_SECRET` - Override DataCrunch client secret
- `AGGREGATOR__SERVER__PORT` - Override server port
- `AGGREGATOR__DATABASE__PATH` - Override database path

Note: Use double underscores (`__`) to navigate nested config structure.

## Development

### Build
```bash
just build-aggregator
```

### Test
```bash
just test-aggregator
```

### Run with custom config
```bash
just aggregator-config /path/to/config.toml
```

## Caching Strategy

- **TTL**: 45 seconds (configurable)
- **Cooldown**: 30 seconds minimum between provider API calls
- **Fallback**: Returns cached data if provider is unavailable
- **Storage**: SQLite database with historical tracking

## Supported GPU Types

- H100 (80GB, 94GB)
- A100 (40GB, 80GB)
- V100 (16GB, 32GB)
- A10 (24GB)
- A6000 (48GB)
- L40 (48GB)
- B200, GH200

## Architecture

```
Provider APIs → Fetcher (cooldown) → SQLite → HTTP API
                                        ↓
                                 Provider modules
                                 (DataCrunch, ...)
```

## Future Phases

- Phase 2: Hyperstack and Lambda Labs integration
- Phase 3: Price trend analysis
- Phase 4: Prometheus metrics, Docker deployment

## License

See root LICENSE file.
