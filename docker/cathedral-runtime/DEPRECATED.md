# cathedral-runtime is DEPRECATED

**As of 2026-05-24, this image is no longer part of the production flow.**

## What replaced it

The current SAT lane uses **direct SSH+Hermes CLI** via `SshHermesRunner`. The
publisher SSHes into the miner's host and runs `hermes chat -q "<task>"`
directly. No HTTP shim, no container required on the miner side.

Relevant env:

```bash
CATHEDRAL_EVAL_MODE=ssh-probe
CATHEDRAL_PROBER_VERSION=v2
```

See `docs/miner/QUICKSTART.md` for the current miner setup.

## Why this image is still here

A handful of v1 callers still reference `cathedral-runtime`:

- `scripts/provision_miner.sh` (provisioner using the old image)
- `tests/test_probe_mode.py` and a couple of `tests/v1/test_polaris_runtime_*.py` files (v1 contract tests)
- `tests/smoke/docker-compose.smoke.yml` (asserts the published tag still exists on ghcr.io)
- `.github/workflows/snyk.yml` (security scan)
- `docs/validator/UPGRADING_TO_V1_1_0.md` and `docs/miner/QUICKSTART_LEGACY_V1_CLAIM.md`

Removing the image cleanly means touching all of those in one shot, which
risks breaking v1 operator workflows during the SAT-lane launch. The
follow-up cleanup PR is tracked separately.

## What's disabled today

`.github/workflows/cathedral-runtime-image.yml` no longer auto-publishes
on push. The image is still buildable via `workflow_dispatch` if an
operator explicitly needs a legacy tag.

## What miners should NOT do

If you are setting up a new miner, **do not deploy this image**. The
endpoint patterns it expects (e.g. `/api/validator/ssh-pubkey-submit`)
are not part of the current architecture and never were. Use the
SSH+Hermes path documented in `docs/miner/QUICKSTART.md` instead.
