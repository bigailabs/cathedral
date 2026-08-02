# Cathedral

Cathedral coordinates verified work for Bittensor SN39. This repository keeps
mechanism, publisher, research, and historical integration code. It is not the
validator operator repository.

## Choose your path

| Goal | Repository |
|---|---|
| Run, audit, or release a validator | [`cathedral-validator`](https://github.com/cathedralai/cathedral-validator) |
| Provide Intel TDX CPU compute | [`cathedral-compute`](https://github.com/cathedralai/cathedral-compute) |
| Compete in the Distill track | [`cathedral-distill`](https://github.com/cathedralai/cathedral-distill) |
| Test the shared command surface | [`cathedral-cli`](https://github.com/cathedralai/cathedral-cli), early beta |
| Use Cathedral Computer | [Product and API documentation](https://cathedral.computer/docs/) |

## Validator authority

[`cathedral-validator`](https://github.com/cathedralai/cathedral-validator) is
the sole source for the validator command, operator guide, configuration,
release bundle, systemd units, runtime policy, dry-run path, and broadcast
gates.

Do not install or run a validator from this repository. This package does not
publish the `cathedral-validator` or `cathedral-thin-validator` console
commands. Validator modules, configs, release scripts, and deployment fixtures
left in this history are retained for mechanism tests and migration review.
They are not supported operator artifacts.

The short [validator routing notice](VALIDATOR.md) points directly to the
canonical operator guide.

## How the system fits together

1. Compute and Distill define admissible work and evidence.
2. Miners perform work and submit evidence for a mechanism.
3. Publisher and mechanism code turn admitted evidence into a signed
   candidate.
4. The canonical validator independently checks the candidate, maps hotkeys to
   current UIDs, applies owner-controlled allocation and burn policy, and
   either refuses or produces one reviewed vector.
5. Only the validator wallet is able to broadcast weights.

Registration, uptime, hardware ownership, attestation, or self-reported volume
never earns weight on its own. Evidence must pass the active validator policy.

## What remains here

- publisher and mechanism code used to form signed evidence and candidates;
- SAT, VerifyML, Violet, arena, and agent-policy experiments;
- miner and contributor tools;
- historical launch records and migration fixtures; and
- local tests for those retained contracts.

A local test, receipt, endpoint, or historical chain row does not prove a lane
is active, admitted, or earning. Check the mechanism repository and the current
validator release before making an operational claim.

## Local development

Use Python 3.11 or 3.12.

```bash
git clone https://github.com/cathedralai/cathedral.git
cd cathedral
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[test]'
python -m pytest -q tests/thin
```

These commands test retained mechanism code. They do not install or exercise a
supported validator.

## Security

Keep wallet seeds, private keys, bearer tokens, cloud credentials, internal
addresses, and controlled evidence out of Git, issues, and public logs.

Treat `PASS`, `FAIL`, and `NOT_PROVEN` as different outcomes. Missing evidence
is not success.

## Licensing

This repository does not currently publish a license file. Do not assume
redistribution rights beyond applicable law.
