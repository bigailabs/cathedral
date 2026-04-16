# Changelog

## 2026-04-16

- Project renamed from Substrate to Cathedral. Repo moved to github.com/bigailabs/cathedral (#8)
- Binaries renamed to cathedral-validator and cathedral-miner (#7)
- Fixed validator bug where verified nodes were marked offline when binary validation is disabled. SSH-based GPU discovery populates gpu_uuid_assignments so nodes stay online without the prover binary (#7)
- Rewrote docs/miner.md for Cathedral with concise quickstart and current validator endpoint (#9)
- First miner fully registered, verified, and scoring 1.0 on the validator (2x A100-SXM4-80GB)

## 2026-04-14

- Published v0 policy doc (docs/policy.md)
- Added status block to README

## 2026-04-13

- Forked from upstream Basilica
- Modified validator to run against self-hosted incentive API
- Rebuilt API endpoints so miners and validators can discover each other on this fork
- Opened PR #1: grpc_endpoint_override config, allow_offline mode on FixedAssignment, non-fatal serve_axon on local nets
- Published live dashboard at polaris.computer/substrate
