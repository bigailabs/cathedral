<p align="center">
  <img src="logo.png" alt="Cathedral" width="200" />
</p>

# <p align="center">Cathedral</p>

<p align="center">
  <em>Compute Layer for Bittensor</em>
</p>

---

<p align="center">
  <a href="docs/miner.md">Miner</a> •
  <a href="docs/validator.md">Validator</a> •
  <a href="docs/architecture.md">Architecture</a> •
  <a href="docs/policy.md">Policy</a>
</p>

## Status

Validator running. Not setting weights yet (stake threshold).
See [docs/policy.md](docs/policy.md) for how the incentive mechanism works.
Live dashboard: https://polaris.computer/substrate

## Why

Bittensor needs a compute layer. Not a product. Not a platform with a token narrative. A base layer that miners provide, validators verify, and applications consume.

Cathedral is a fork of [Basilica](https://github.com/one-covenant/basilica), one of the strongest compute codebases ever built on Bittensor. When its original team walked away in April 2026, the architecture survived. The code was sound. The miners and builders who believed in it stayed.

Cathedral carries that work forward.

## Overview

Cathedral creates a trustless marketplace for GPU compute by:

- **Hardware Verification**: Validates GPUs through a mix of binary challenges and SSH-based discovery, with cryptographic attestation where available
- **Remote Validation**: SSH-based verification of hardware, network, storage, and container runtime
- **Bittensor Integration**: Native participation in consensus, weight-setting, and emission distribution on Subnet 39
- **Fleet Management**: Orchestration of distributed GPU nodes across miners, with exclusive access enforcement
- **Incentive Engine**: CU and RU dual-stream payouts, linear vesting, and category-based dilution — documented in [docs/policy.md](docs/policy.md)

## Key Components

- **Validator**: Verifies hardware, scores miners, computes weights, submits to chain
- **Miner**: Registers GPU nodes with validators, deploys validator SSH keys, serves health checks
- **Executor**: GPU machine agent for container lifecycle, system monitoring, and secure task execution
- **API Gateway**: HTTP gateway with authentication, caching, and request aggregation
- **Common**: Shared utilities — crypto, SSH, persistence, identity, config
- **Protocol**: gRPC/protobuf definitions for inter-component communication

## Network Information

- **Mainnet**: Bittensor Finney, Subnet 39
- **Chain Endpoint**: `wss://entrypoint-finney.opentensor.ai:443`

## Origins

Forked from [one-covenant/basilica](https://github.com/one-covenant/basilica) under the MIT License. The original Basilica code was written by the Covenant team between 2025 and 2026. Their work is what made this possible.

## License

MIT
