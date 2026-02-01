# Basilica Localnet - Composable Local Development

A modular, composable Docker-based local development environment for end-to-end testing of Basilica services.

## Quick Start

```bash
# One-time setup (SSH keys, pull images)
./setup.sh

# Start Subtensor
./start.sh network

# Initialize subnet (creates wallets, funds, registers neurons)
./init-subnet.sh

# Start remaining services
./start.sh miner

# Check service health
./test.sh

# Stop all services
docker compose down
```

## Workflow Steps

### 1. Initial Setup

```bash
./setup.sh
```

This creates SSH keys and pulls Docker images. Only needs to be run once.

### 2. Start Subtensor

```bash
./start.sh network
```

Starts the local Bittensor blockchain. Wait for it to be healthy before proceeding.

### 3. Initialize Subnet

```bash
./init-subnet.sh
```

This script:
- Creates wallets in `./wallets/` directory (self-contained, not in `~/.bittensor`)
- Funds wallets via local faucet
- Creates subnet (netuid=1)
- Registers validator and miner neurons
- Adds stake to validator

### 4. Start Services

```bash
# Start validator + miner
./start.sh miner

# Or with monitoring
./start.sh monitoring
```

## Composable Profiles

Start only the services you need:

```bash
# Just the blockchain
./start.sh network

# Blockchain + Database + Validator
./start.sh validator

# Above + Miner
./start.sh miner

# Full stack with monitoring
./start.sh monitoring

# Everything (default)
./start.sh all
```

### Profile Matrix

| Profile | subtensor | postgres | validator | miner | prometheus | grafana |
|---------|-----------|----------|-----------|-------|------------|---------|
| `network` | x | | | | | |
| `validator` | x | x | x | | | |
| `miner` | x | x | x | x | | |
| `monitoring` | x | x | x | x | x | x |

## Services

| Service | Ports | Description |
|---------|-------|-------------|
| **subtensor** | 9944 (WS) | Local Bittensor blockchain |
| **postgres** | 5432 | Validator database |
| **validator** | 8080 (API), 9090 (metrics) | Verification service |
| **miner** | 8092 (gRPC), 8091 (axon), 9091 (metrics) | GPU node manager |
| **prometheus** | 9099 | Metrics collection |
| **grafana** | 3000 | Metrics visualization |

## Endpoints

Once running, access services at:

- **Subtensor WebSocket**: `ws://localhost:9944`
- **Validator API**: http://localhost:8080
- **Validator Metrics**: http://localhost:9090/metrics
- **Miner gRPC**: `localhost:8092`
- **Miner Metrics**: http://localhost:9091/metrics
- **Prometheus**: http://localhost:9099
- **Grafana**: http://localhost:3000 (admin/admin)

## Verification

```bash
# Check wallets were created locally
ls ./wallets/

# Check subnet exists
uvx --from bittensor-cli btcli subnet list --network local

# Check registrations
uvx --from bittensor-cli btcli subnet metagraph --netuid 1 --network local

# Check validator health
curl http://localhost:8080/health

# Check miner metrics
curl http://localhost:9091/metrics
```

## Development Workflow

### Rebuild after code changes

```bash
# Rebuild and restart all
./start.sh --build

# Rebuild specific service
docker compose up -d --build validator
```

### View logs

```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f validator
docker compose logs -f miner
```

### Restart services

```bash
# Restart all
docker compose restart

# Restart specific
docker compose restart validator
```

## Directory Structure

```
scripts/localnet/
├── docker-compose.yml    # All services with profiles
├── configs/
│   ├── validator.toml    # Validator config (local network)
│   ├── miner.toml        # Miner config (local network)
│   └── prometheus.yml    # Prometheus scrape config
├── setup.sh              # One-time setup (SSH keys, images)
├── init-subnet.sh        # Subnet initialization (wallets, funding, registration)
├── start.sh              # Start services by profile
├── test.sh               # Health check script
├── wallets/              # Self-contained wallets (gitignored)
├── ssh-keys/             # Generated SSH keys (gitignored)
└── README.md             # This file
```

## Configuration

### Validator (`configs/validator.toml`)

Pre-configured for local development:
- Network: `local`
- Chain endpoint: `ws://subtensor:9944`
- Database: PostgreSQL (`postgres://basilica:basilica@postgres:5432/validator`)
- Wallet: `validator` / `default`

### Miner (`configs/miner.toml`)

Pre-configured for local development:
- Network: `local`
- Chain endpoint: `ws://subtensor:9944`
- Database: SQLite (embedded)
- Wallet: `miner_1` / `default`

## Wallets

Wallets are created in `./wallets/` (self-contained, not in `~/.bittensor`):

| Wallet | Purpose |
|--------|---------|
| `owner` | Subnet creation |
| `validator` | Validator service identity |
| `miner_1` | Miner service identity |

To create additional wallets:
```bash
uvx --from bittensor-cli btcli wallet new_coldkey \
    --wallet.name my_wallet \
    --wallet.path ./wallets

uvx --from bittensor-cli btcli wallet new_hotkey \
    --wallet.name my_wallet \
    --wallet.hotkey default \
    --wallet.path ./wallets
```

## Troubleshooting

### Services not starting

1. Check Docker is running: `docker info`
2. View service logs: `docker compose logs -f [service]`
3. Ensure wallets exist: `ls ./wallets/`

### Connection refused errors

1. Wait for services to be ready (healthchecks take time)
2. Run `./test.sh` to see which services are unhealthy
3. Check if ports are in use: `lsof -i :8080`

### Rebuild from scratch

```bash
# Stop and remove all containers, volumes
docker compose down -v

# Remove built images
docker compose down --rmi local

# Remove local wallets
rm -rf ./wallets/

# Start fresh
./setup.sh
./start.sh network
./init-subnet.sh
./start.sh miner
```

### Database issues

Reset PostgreSQL:
```bash
docker compose down
docker volume rm basilica-localnet_postgres-data
./start.sh validator
```

### Subtensor not syncing

The local Subtensor starts fresh each time. If you need persistent state:
```bash
# Data is stored in the subtensor-data volume
docker volume inspect basilica-localnet_subtensor-data
```

## Legacy Setup

The previous localnet configuration is preserved in `scripts/localnet-old/` for reference.
