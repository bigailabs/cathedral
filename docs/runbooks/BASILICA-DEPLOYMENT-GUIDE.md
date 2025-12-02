# Basilica Component Deployment Guide

## Overview

This guide provides step-by-step instructions for building and deploying the Basilica components (validator, miner, and executor) using the automated deployment script `scripts/deploy.sh`.

## ⚠️ CRITICAL REQUIREMENT

**ZERO TOLERANCE POLICY**: This deployment process requires 100% production-ready, fully functional code. NO fake implementations, TODO items, mock functions, stubs, placeholders, or incomplete features are acceptable. Every component must be completely implemented and working before deployment.

## Prerequisites

Before starting, ensure you have:

- Access to the Basilica repository
- Rust toolchain installed (rustc 1.75+)
- SSH access to deployment servers
- Configured SSH keys for server access

## Automated Deployment Script

The repository includes an automated deployment script at `scripts/deploy.sh` that handles building, deploying, and managing all Basilica components.

### Script Features

- **Automated Building**: Uses component-specific build scripts
- **Remote Deployment**: Deploys to multiple servers simultaneously
- **Wallet Synchronization**: Syncs Bittensor wallets to remote servers
- **Health Checks**: Verifies service status post-deployment
- **Log Following**: Stream logs from deployed services
- **SSH Access Setup**: Configures miner-executor SSH connectivity

## Quick Start

> **Note**: Production server addresses and credentials are stored in AWS Secrets Manager.
> See `basilica/production/deployment-targets` for connection details.

### Deploy All Services

```bash
# Deploy all services (validator, miner, executor)
./scripts/deploy.sh -s all \
  -v root@<VALIDATOR_HOST>:<VALIDATOR_PORT> \
  -m root@<MINER_HOST>:<MINER_PORT> \
  -e <EXECUTOR_USER>@<EXECUTOR_HOST>:<EXECUTOR_PORT>
```

### Deploy Individual Services

```bash
# Deploy only validator
./scripts/deploy.sh -s validator -v root@<VALIDATOR_HOST>:<VALIDATOR_PORT>

# Deploy only miner with wallet sync
./scripts/deploy.sh -s miner -m root@<MINER_HOST>:<MINER_PORT> -w

# Deploy validator and miner with health checks
./scripts/deploy.sh -s validator,miner \
  -v root@<VALIDATOR_HOST>:<VALIDATOR_PORT> \
  -m root@<MINER_HOST>:<MINER_PORT> \
  -c
```

## Server Configuration

### Target Servers

Server connection details are stored in AWS Secrets Manager under `basilica/production/deployment-targets`.

1. **Validator Server**: `root@<VALIDATOR_HOST>` (port `<VALIDATOR_PORT>`)
2. **Miner Server**: `root@<MINER_HOST>` (port `<MINER_PORT>`)
3. **Executor Server**: `<EXECUTOR_USER>@<EXECUTOR_HOST>` (port `<EXECUTOR_PORT>`)

## Command Line Options

The deployment script supports various options for customization:

```bash
Usage: ./scripts/deploy.sh [OPTIONS]

OPTIONS:
    -s, --services SERVICES      Comma-separated list: validator,miner,executor or 'all'
    -v, --validator USER@HOST:PORT    Validator server connection
    -m, --miner USER@HOST:PORT        Miner server connection
    -e, --executor USER@HOST:PORT     Executor server connection
    -w, --sync-wallets               Sync local wallets to remote servers
    -f, --follow-logs                Stream logs after deployment
    -c, --health-check               Perform health checks on service endpoints
    -t, --timeout SECONDS           SSH timeout (default: 60)
    -b, --veritas-binaries DIR       Directory containing veritas binaries to deploy
    -h, --help                       Show this help
```

## Deployment Process

The script performs the following steps automatically:

### 1. Build Phase

- Uses component-specific build scripts (`scripts/{service}/build.sh`)
- For validator builds with veritas binaries: passes `--veritas-binaries` flag to include them in Docker image
- Creates binaries in the repository root (not `target/release/`)
- Validates that binaries are created successfully

### 2. Wallet Synchronization (Optional)

- Syncs `test_validator` wallet to validator server
- Syncs `test_miner` wallet to miner server
- Sets proper permissions (700 for directories, 600 for JSON files)

### 3. Service Deployment

For each service, the script:

- Stops existing processes gracefully, then forcefully if needed
- Backs up existing binaries
- Copies new binaries to `/opt/basilica/`
- Sets executable permissions
- Copies configuration files from `config/{service}.correct.toml`
- Creates necessary data directories  
- Deploys veritas binaries if specified (for validator/executor services)
- Starts services with proper logging

### 4. SSH Access Setup (Miner + Executor)

- Creates SSH key pair on miner server
- Adds miner's public key to executor's authorized_keys
- Tests SSH connectivity between miner and executor

### 5. Health Checks (Optional)

- Verifies services are running
- Checks configured ports
- Reports service status

## Veritas Binaries Integration

### Overview

The deployment script supports deploying veritas binaries alongside the main validator and executor services. These binaries are used for GPU validation and verification tasks.

### Veritas Binaries Structure

The veritas binaries directory should contain:
```
../veritas/binaries/
├── executor-binary/
│   └── executor-binary          # GPU executor binary
└── validator-binary/
    └── validator-binary         # GPU validator binary
```

### Deployment Process

When using the `--veritas-binaries` option:

1. **Validation**: Script validates that both binaries exist in the specified directory
2. **Docker Build**: For validator builds, binaries are included in the Docker image
3. **Direct Deployment**: Binaries are copied to `/opt/basilica/bin/` on target servers
4. **Permissions**: Executable permissions are set on both binaries

### Configuration Integration

The deployed binaries are automatically available at:
- `/opt/basilica/bin/executor-binary`
- `/opt/basilica/bin/validator-binary`

These paths are pre-configured in the validator configuration file (`config/validator.correct.toml`):
```toml
[verification.binary_validation]
enabled = true
validator_binary_path = "/opt/basilica/bin/validator-binary"
executor_binary_path = "/opt/basilica/bin/executor-binary"
execution_timeout_secs = 120
remote_executor_path = "/tmp/executor-binary"
output_format = "json"
```

### Usage Examples

```bash
# Deploy validator with veritas binaries
./scripts/deploy.sh -s validator \
  -v root@<VALIDATOR_HOST>:<VALIDATOR_PORT> \
  -b ../veritas/binaries

# Deploy all services with veritas binaries and health checks
./scripts/deploy.sh -s all \
  -v root@<VALIDATOR_HOST>:<VALIDATOR_PORT> \
  -m root@<MINER_HOST>:<MINER_PORT> \
  -e <EXECUTOR_USER>@<EXECUTOR_HOST>:<EXECUTOR_PORT> \
  -b ../veritas/binaries \
  -c
```

### Verification

After deployment, verify the binaries are correctly deployed:
```bash
# Check binaries exist and are executable
ssh root@<VALIDATOR_HOST> -p <VALIDATOR_PORT> "ls -la /opt/basilica/bin/"

# Test binary execution
ssh root@<VALIDATOR_HOST> -p <VALIDATOR_PORT> "/opt/basilica/bin/validator-binary --help"
```

## Service-Specific Configuration

> **Note**: Wallet credentials are stored in AWS Secrets Manager under `basilica/production/wallets`.

### Validator

- Started with: `sudo ./validator --start --config config/validator.toml`
- Logs to: `/opt/basilica/validator.log`
- Configuration: `/opt/basilica/config/validator.toml`
- Wallet: `test_validator` (coldkey: `<VALIDATOR_COLDKEY>`, hotkey: `<VALIDATOR_HOTKEY>`)

### Miner

- Started with: `sudo ./miner --config config/miner.toml`
- Logs to: `/opt/basilica/miner.log`
- Configuration: `/opt/basilica/config/miner.toml`
- SSH key: `/root/.ssh/miner_executor_key`
- Database: `/opt/basilica/data/miner.db`
- Wallet: `test_miner` (coldkey: `<MINER_COLDKEY>`, hotkey: `<MINER_HOTKEY>`)

### Executor

- Started with: `sudo ./executor --server --config config/executor.toml`
- Logs to: `/opt/basilica/executor.log`
- Configuration: `/opt/basilica/config/executor.toml`
- **CRITICAL**: Requires sudo/root privileges for container management and system access
- **CRITICAL**: Must listen on port 50051 for gRPC communication with miner
- **CRITICAL**: Must restart service if configuration IP addresses change
- No wallet required

## Advanced Usage Examples

### Deploy with Wallet Sync and Health Checks

```bash
# Deploy all services with wallet sync and health monitoring
./scripts/deploy.sh -s all \
  -v root@<VALIDATOR_HOST>:<VALIDATOR_PORT> \
  -m root@<MINER_HOST>:<MINER_PORT> \
  -e <EXECUTOR_USER>@<EXECUTOR_HOST>:<EXECUTOR_PORT> \
  -w -c
```

### Deploy and Follow Logs

```bash
# Deploy all services and stream logs afterwards
./scripts/deploy.sh -s all \
  -v root@<VALIDATOR_HOST>:<VALIDATOR_PORT> \
  -m root@<MINER_HOST>:<MINER_PORT> \
  -e <EXECUTOR_USER>@<EXECUTOR_HOST>:<EXECUTOR_PORT> \
  -f
```

### Deploy with Custom Timeout

```bash
# Deploy with extended timeout for slow connections
./scripts/deploy.sh -s all \
  -v root@<VALIDATOR_HOST>:<VALIDATOR_PORT> \
  -m root@<MINER_HOST>:<MINER_PORT> \
  -e <EXECUTOR_USER>@<EXECUTOR_HOST>:<EXECUTOR_PORT> \
  -t 120
```

### Deploy with Veritas Binaries

```bash
# Deploy validator with veritas binaries
./scripts/deploy.sh -s validator \
  -v root@<VALIDATOR_HOST>:<VALIDATOR_PORT> \
  -b ../veritas/binaries

# Deploy all services with veritas binaries
./scripts/deploy.sh -s all \
  -v root@<VALIDATOR_HOST>:<VALIDATOR_PORT> \
  -m root@<MINER_HOST>:<MINER_PORT> \
  -e <EXECUTOR_USER>@<EXECUTOR_HOST>:<EXECUTOR_PORT> \
  -b ../veritas/binaries
```

## Manual Verification Commands

### Check Service Status

```bash
# Check validator status
ssh root@<VALIDATOR_HOST> -p <VALIDATOR_PORT> "pgrep -f validator"

# Check miner status
ssh root@<MINER_HOST> -p <MINER_PORT> "pgrep -f miner"

# Check executor status
ssh <EXECUTOR_USER>@<EXECUTOR_HOST> "pgrep -f executor"
```

### View Service Logs

```bash
# View validator logs
ssh root@<VALIDATOR_HOST> -p <VALIDATOR_PORT> "tail -f /opt/basilica/validator.log"

# View miner logs
ssh root@<MINER_HOST> -p <MINER_PORT> "tail -f /opt/basilica/miner.log"

# View executor logs
ssh <EXECUTOR_USER>@<EXECUTOR_HOST> "tail -f /opt/basilica/executor.log"
```

### Test SSH Connectivity

```bash
# Test miner to executor SSH access
ssh root@<MINER_HOST> -p <MINER_PORT> "ssh -i /root/.ssh/miner_executor_key -o StrictHostKeyChecking=no <EXECUTOR_USER>@<EXECUTOR_HOST> 'echo SSH test successful'"
```

## Configuration Management

### Configuration Files

The script uses configuration files from `config/{service}.correct.toml`:

- `config/validator.correct.toml` → `/opt/basilica/config/validator.toml`
- `config/miner.correct.toml` → `/opt/basilica/config/miner.toml`
- `config/executor.correct.toml` → `/opt/basilica/config/executor.toml`

### Wallet Management

Local wallets are synchronized from `~/.bittensor/wallets/`:

- `test_validator` wallet → validator server
- `test_miner` wallet → miner server

## Troubleshooting

### Common Issues

1. **SSH Connection Timeouts**: Increase timeout with `-t` option
2. **Binary Not Found**: Ensure build scripts complete successfully
3. **Permission Denied**: Verify SSH key access to target servers
4. **Service Won't Start**: Check log files for specific error messages
5. **Executor Transport Errors**: Executor failing to bind to port 50051
6. **Miner Health Check Failures**: Miner cannot reach executor due to wrong IP configuration
7. **Executor Permission Issues**: Executor requires root privileges to manage containers

### Error Recovery

If deployment fails:

1. Check the last log output for specific errors
2. Verify SSH connectivity to target servers
3. Ensure configuration files exist and are valid
4. Check that binary files were created successfully

### Critical Deployment Issues and Solutions

#### 1. Executor Transport Errors (gRPC server error: transport error)

**Symptoms:**
- Executor logs show: `gRPC server error: transport error`
- Port 50051 already in use by previous executor process

**Solution:**
```bash
# Kill existing executor processes
ssh <EXECUTOR_USER>@<EXECUTOR_HOST> "pkill -f executor"

# Verify port is free
ssh <EXECUTOR_USER>@<EXECUTOR_HOST> "ss -tlnp | grep 50051"

# Restart executor with sudo
ssh <EXECUTOR_USER>@<EXECUTOR_HOST> "cd /opt/basilica && sudo nohup ./executor --server --config config/executor.toml > executor.log 2>&1 &"
```

#### 2. Miner Health Check Failures (transport error)

**Symptoms:**
- Miner logs show: `Health check failed for executor: transport error`
- Miner trying to connect to wrong IP address

**Solution:**
```bash
# Verify executor configuration has correct IP
ssh root@<MINER_HOST> -p <MINER_PORT> "grep -n '<EXECUTOR_HOST>' /opt/basilica/config/miner.toml"

# If configuration is wrong, update and restart miner
ssh root@<MINER_HOST> -p <MINER_PORT> "pkill -f miner"
ssh root@<MINER_HOST> -p <MINER_PORT> "cd /opt/basilica && ./miner --config config/miner.toml > miner.log 2>&1 &"
```

#### 3. Executor Permission Issues

**Symptoms:**
- Executor cannot start containers
- Container management failures in logs

**Solution:**
```bash
# Ensure executor runs with root privileges
ssh <EXECUTOR_USER>@<EXECUTOR_HOST> "ps aux | grep executor"

# If not running as root, restart with sudo
ssh <EXECUTOR_USER>@<EXECUTOR_HOST> "pkill -f executor"
ssh <EXECUTOR_USER>@<EXECUTOR_HOST> "cd /opt/basilica && sudo nohup ./executor --server --config config/executor.toml > executor.log 2>&1 &"
```

#### 4. Configuration IP Address Mismatches

**Symptoms:**
- Services deployed but cannot communicate
- Health checks failing between miner and executor

**Solution:**
```bash
# Update executor configuration files with correct IP
# Miner config: config/miner.correct.toml
# Executor config: config/executor.correct.toml

# Redeploy with updated configurations
./scripts/deploy.sh -s miner,executor -m root@<MINER_HOST>:<MINER_PORT> -e <EXECUTOR_USER>@<EXECUTOR_HOST>:<EXECUTOR_PORT>
```

### Manual Rollback

If needed, manually restore from backups:

```bash
# Restore validator
ssh root@<VALIDATOR_HOST> -p <VALIDATOR_PORT> "pkill -f validator; mv /opt/basilica/validator.backup /opt/basilica/validator"

# Restore miner
ssh root@<MINER_HOST> -p <MINER_PORT> "pkill -f miner; mv /opt/basilica/miner.backup /opt/basilica/miner"

# Restore executor
ssh <EXECUTOR_USER>@<EXECUTOR_HOST> "pkill -f executor; sudo mv /opt/basilica/executor.backup /opt/basilica/executor"
```

## Security Considerations

1. **Credential Storage**: All production credentials (server IPs, wallet keys, API tokens) are stored in AWS Secrets Manager:
   - `basilica/production/deployment-targets`: Server connection details
   - `basilica/production/wallets`: Wallet coldkeys and hotkeys
   - Access via AWS CLI: `aws secretsmanager get-secret-value --secret-id basilica/production/deployment-targets`
2. **SSH Keys**: The script manages SSH keys for miner-executor communication
3. **Wallet Security**: Wallet files are secured with restrictive permissions (700 for directories, 600 for files)
4. **Process Management**: Services are stopped gracefully before deployment
5. **Backup Strategy**: Previous binaries are backed up before replacement
6. **Access Control**: Limit access to production credentials to authorized personnel only

## Post-Deployment Verification

After successful deployment, verify all services are working correctly:

### 1. Service Status Check
```bash
# Verify all services are running from correct locations
echo "Validator:" && ssh root@<VALIDATOR_HOST> -p <VALIDATOR_PORT> "pgrep -f '/opt/basilica/validator'"
echo "Miner:" && ssh root@<MINER_HOST> -p <MINER_PORT> "pgrep -f '/opt/basilica/miner'"
echo "Executor:" && ssh <EXECUTOR_USER>@<EXECUTOR_HOST> "pgrep -f '/opt/basilica/executor'"
```

### 2. Executor Root Permissions Check
```bash
# Confirm executor is running with root privileges
ssh <EXECUTOR_USER>@<EXECUTOR_HOST> "ps aux | grep -v grep | grep 'root.*executor'"
```

### 3. Network Connectivity Verification
```bash
# Test gRPC port accessibility
ssh root@<MINER_HOST> -p <MINER_PORT> "timeout 5 bash -c '</dev/tcp/<EXECUTOR_HOST>/50051' && echo 'Executor gRPC port reachable'"

# Test SSH connectivity
ssh root@<MINER_HOST> -p <MINER_PORT> "ssh -i /root/.ssh/miner_executor_key -o StrictHostKeyChecking=no -o ConnectTimeout=5 <EXECUTOR_USER>@<EXECUTOR_HOST> 'echo \"SSH connectivity verified\"'"
```

### 4. Health Check Verification
```bash
# Check miner logs for successful health checks
ssh root@<MINER_HOST> -p <MINER_PORT> "tail -10 /opt/basilica/miner.log | grep -i 'health.*healthy'"

# Check executor logs for health check responses
ssh <EXECUTOR_USER>@<EXECUTOR_HOST> "tail -10 /opt/basilica/executor.log | grep -i 'health check requested'"
```

## Best Practices

1. **Test Deployments**: Always test with individual services before deploying all
2. **Monitor Logs**: Use `-f` option to monitor deployment progress
3. **Health Checks**: Use `-c` option to verify service health post-deployment
4. **Coordinate Updates**: Ensure all team members are aware of deployments
5. **Document Changes**: Keep track of configuration changes and deployment history
6. **Verify Root Permissions**: Always confirm executor is running with sudo/root privileges
7. **Check Service Communication**: Verify miner-executor gRPC communication is working
8. **Monitor Health Checks**: Ensure continuous health check success between services
