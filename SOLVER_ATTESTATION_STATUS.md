# Solver Attestation Status

Status: default-off publisher endpoints exist; real DCAP verification and the
production runner have env-gated integration seams. They are not active unless an
operator configures real verifier/runner commands.

## What Exists

The solver-attestation work is real, but it is split across prototype, spec, and
active publisher code:

- `C:\Users\fred\code\cathedral-scaffold\ATTESTATION.md` defines the Route B
  attestation binding.
- `C:\Users\fred\code\cathedral-scaffold\RECEIPT-LANE-SPEC.md` gives the deeper
  receipt-lane plan and adversarial review.
- `scaffold/publisher/attest.py` implements nonce issuance and attestation
  verification as callable Python functions.
- `scaffold/lanes/solver_docker.py` implements the offline solver-docker lane:
  SAT witness verification, optional attested speed credit, and attested timeout
  claims.
- `scaffold/lanes/solver_arena.py`, `scaffold/publisher/arena_eval.py`, and
  `scaffold/publisher/arena_payout.py` implement the solver arena mechanics.
- The active publisher exposes:
  - `POST /v1/arena/solvers`
  - `GET /v1/arena/status`
  - `POST /v1/arena/instances`
- `POST /v1/attest/nonce`
- `POST /v1/attest`
- `GET /v1/attest/status/{eval_run_id}`
- The active DB migrations create:
  - `attest_nonces`
  - `attestations`
  - `eval_runs.attested`

The attestation endpoints are safe by default:

- `CATHEDRAL_ATTEST_ENABLED` unset: endpoints return 404.
- `POST /v1/attest/nonce` requires `challenge_id` and `miner_pubkey_b64`; the
  pubkey is stored with the nonce and must match the quote payload at verify.
- `CATHEDRAL_ATTEST_ENABLED=1` without a verifier command: `/v1/attest` returns 503.
- `CATHEDRAL_ATTEST_DCAP_VERIFY_CMD=<cmd>`: verify uses the configured real
  command. Default call shape is `<cmd> quote.bin expected_report_data_hex out.json`.
- `CATHEDRAL_ATTEST_ALLOW_STUB=1`: test/shadow-only stub verification is allowed.
  It is ignored when `CATHEDRAL_ENV=production|prod|mainnet` or
  `CATHEDRAL_PRODUCTION=1`.
- `POST /v1/attest` requires the same signed hotkey headers as nonce issuance
  once a verifier is configured.
- `GET /v1/attest/status/{eval_run_id}` is private by default. Set
  `CATHEDRAL_ATTEST_STATUS_TOKEN` and send `Authorization: Bearer ...`, or set
  `CATHEDRAL_ATTEST_STATUS_PUBLIC=1` only for a deliberately public deployment.
- A verified attestation upgrades and re-signs the bound `eval_runs` row. It
  affects emissions only when the configured weight composition consumes that
  row's `weighted_score`, or through an explicit mode wired elsewhere.

The production arena eval loop is deliberately safe-defaulted. It now resolves
runner capability through `scaffold/publisher/arena_runner.py`:

- no runner env: `_prod_adapter_for(spec)` returns `None`.
- `CATHEDRAL_ARENA_RUNNER_CMD=<cmd>`: uses an operator wrapper command.
  Wrapper commands should put miner-controlled placeholders after a `--` or
  otherwise treat them as values, not option flags.
- `CATHEDRAL_ARENA_SOLVER_BIN=<path>`: uses a local solver binary.
- `CATHEDRAL_ARENA_REQUIRE_CONTAINMENT=1` by default; if sandbox containment is
  unavailable, no adapter is returned and solvers stay pending.
- `champion_provider` returns `None`.
- Turning the arena loops on does not run real containers or pay record-fall
  claims unless a runner is configured.

Plain English:

> The solver-attestation design and verifier primitives exist. The live
> publisher endpoint shell now exists. Cathedral can call real verifier and
> runner commands, but the actual production DCAP binary/wrapper deployment is
> still an ops/config gate.

## Finish Path

1. Configure and pin a real DCAP/TDX verifier command in production.
2. Configure and pin a real arena runner wrapper for container-digest execution.
3. Keep the routes fail-closed unless the real DCAP/TDX verifier is configured,
   with stub verification allowed only in tests or shadow mode.
4. Require attestation only for producer identity, timeout/hardness, title, or
   multiplier claims; keep ordinary SAT correctness certificate-verified.
5. Extend tests with real golden DCAP quote fixtures and production runner
   receipts.
6. Gate or redact any additional attestation status fields before enabling the
   lane broadly.
