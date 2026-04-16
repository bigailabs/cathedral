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
  <a href="docs/architecture.md">Architecture</a>
</p>

## Status

Validator running. Not setting weights yet (stake threshold).
See [docs/policy.md](docs/policy.md) for how this works.
Live dashboard: https://polaris.computer/cathedral

## Why

Bittensor needs a compute layer. Not a product. Not a platform with a token narrative. A cathedral -- infrastructure that miners provide, validators verify, and applications consume.

This project is a fork of [Basilica](https://github.com/one-covenant/basilica), one of the strongest compute codebases ever built on Bittensor. When its original team walked away in April 2026, the architecture survived. The code was sound. The miners and builders who believed in it stayed.

Cathedral carries that work forward.

## Overview

Cathedral creates a trustless marketplace for GPU compute by:

- **Hardware Verification**: Binary validation system for secure GPU verification and profiling
- **Remote Validation**: SSH-based verification of computational tasks and hardware specifications
- **Bittensor Integration**: Native participation in Bittensor's consensus mechanism with weight allocation
- **Fleet Management**: Efficient orchestration of distributed GPU resources with assignment management
- **Cathedral API Gateway**: Smart HTTP gateway providing load-balanced access to the validator network

## Key Components

- **Validator**: Verifies hardware capabilities, maintains GPU profiles, and scores miner performance
- **Miner**: Manages GPU executor fleets, handles assignments, and serves compute requests via Axon
- **Executor**: GPU machine agent with container management, system monitoring, and secure task execution
- **Cathedral API**: HTTP gateway with authentication, caching, rate limiting, and request aggregation
- **Cathedral Common**: Shared utilities including crypto, SSH management, storage, and configuration
- **Protocol**: gRPC/protobuf definitions for inter-component communication
- **Bittensor**: Network integration for registration, discovery, and weight management

## Network Information

- **Mainnet**: Bittensor Finney, Subnet 39
- **Chain Endpoint**: `wss://entrypoint-finney.opentensor.ai:443` (mainnet)

## Origins

Forked from [one-covenant/basilica](https://github.com/one-covenant/basilica) under MIT License. Original work by the Basilica contributors. Continued by the community.

## License

MIT
