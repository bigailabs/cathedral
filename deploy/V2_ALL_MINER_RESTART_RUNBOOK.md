# V2 All-Miner Restart Runbook

Goal: restore V2 miner access without a stampede taking down the sandbox origin
or silently changing the reward mechanism. This runbook assumes the Cloudflare
worker is deployed in staged mode by default and only opens all miners when
`V2_GATE_MODE=open-v2` is deployed explicitly.

## Invariants

- Do not open the gate until the operator explicitly decides to do so.
- Keep V1 miner paths retired.
- Keep public verifier disabled; only the private verifier drains V2 receipts.
- Keep the live environment equal to `deploy/sandbox/env.template.sh`.
- Decide `CATHEDRAL_V2_REAL_FRACTION` before opening. Use `0` for all-planted
  conservative restart or `0.10` only if that is the deliberate mechanism call.
- Treat a clean staged/canary proof as permission to ramp, not as proof that an
  immediate all-miner flood is safe.
- All-miner open is NO-GO until either verifier drain is measured at or above
  the launch target with headroom, or V2 submit backpressure is enabled and
  tested.

## Current All-Miner Blockers

Do not proceed to all-miner open until these are closed:

- Verify drain: run a synthetic backlog/drain measurement large enough to prove
  the private verifier can drain faster than projected arrival. The target is
  measured drain at least 2x expected arrival, not an estimate from batch size.
- V2 submit shedding: enable and test
  `CATHEDRAL_V2_SUBMIT_BACKPRESSURE_ENABLED=true` so new bitset submits receive
  `503 v2_submit_backpressure` plus `Retry-After` when pending work is unsafe.
- Sustained load: run a 5-minute closed/canary load test before opening. Green
  means no 5xx, submit admit p99 inside the chosen threshold, verifier drain
  keeps pace, and weight-vector publication stays fresh.
- Mechanism decision: `CATHEDRAL_V2_REAL_FRACTION` must match the intended
  launch mechanism in the template and running process.
- Miner comms: announce the V2 client path before opening, including the
  requirement to read `X-Cathedral-Submit-Token` from the CNF fetch response and
  to honor `Retry-After`.

## Required Inputs

Set these locally before running the launch preflight:

```sh
export CATHEDRAL_PREFLIGHT_SSH=<operator>@<sandbox-host>
export CATHEDRAL_PREFLIGHT_SSH_KEY=<path-to-ssh-key>
export CATHEDRAL_PREFLIGHT_EXPECTED_V2_REAL_FRACTION=0
```

If the launch decision is to return to the historical real mix, use:

```sh
export CATHEDRAL_PREFLIGHT_EXPECTED_V2_REAL_FRACTION=0.10
```

## 1. Keep The Edge Closed

Confirm the deployed worker is still staged:

```sh
python deploy/v2-beta-router/relaunch_preflight.py \
  --launch-intent staged \
  --run-edge-soak
```

Expected result:

```text
PASS non-canary V2 gate closed
PASS staged edge soak
PRECHECK_OK
```

## 2. Bake Current And Next Epoch

Run this on the sandbox host, with the same env file used by the serving
processes:

```sh
cd /home/polaris/cathedral
set -a
. ./.env.sh
set +a

CURRENT_EPOCH=$(.venv/bin/python - <<'PY'
from scaffold.publisher import per_miner as pm
print(pm.current_epoch())
PY
)

.venv/bin/python deploy/sandbox/prebake_perminer_cnfs.py --depth 10 --epoch "$CURRENT_EPOCH"
.venv/bin/python deploy/sandbox/prebake_perminer_cnfs.py --depth 10 --epoch "$((CURRENT_EPOCH + 1))"
```

Both commands must end with `failed=0`. Re-running is safe; already-baked rows
are skipped.

Do not open during the epoch rollover window. Target `:10` through `:40` past
the hour and keep a dedicated watch on the first epoch boundary after open.

## 3. Open Split Tunnels For Capacity Proof

From the local operator machine:

```sh
ssh -i "$CATHEDRAL_PREFLIGHT_SSH_KEY" \
  -o BatchMode=yes \
  -o ConnectTimeout=10 \
  -f -N \
  -L 18080:127.0.0.1:8000 \
  -L 18081:127.0.0.1:8080 \
  "$CATHEDRAL_PREFLIGHT_SSH"
```

`18081` reaches the public sandbox process for reads/submits. `18080` reaches
the private verifier metrics process.

## 4. Run Strict All-Miner Preflight

```sh
python deploy/v2-beta-router/relaunch_preflight.py \
  --launch-intent all-miner-open \
  --expected-v2-real-fraction "$CATHEDRAL_PREFLIGHT_EXPECTED_V2_REAL_FRACTION" \
  --run-e2e \
  --run-capacity-probe \
  --run-edge-soak \
  --prebake-epoch-lookahead 1 \
  --capacity-challenge-base http://127.0.0.1:18081 \
  --capacity-submit-base http://127.0.0.1:18081 \
  --capacity-metrics-base http://127.0.0.1:18080 \
  --capacity-miners 4 \
  --capacity-per-miner-limit 4 \
  --capacity-submit-concurrency 8 \
  --capacity-max-drain-secs 25 \
  --capacity-max-admit-p95-ms 1500 \
  --edge-soak-requests 64 \
  --edge-soak-concurrency 16 \
  --edge-soak-max-p95-ms 1500
```

Go only if the result is:

```text
PRECHECK_OK
PASS all-miner launch intent prerequisites
PASS remote runtime guardrails
PASS remote CNF prebake coverage
PASS canary V2 bitset E2E
PASS V2 capacity probe
PASS staged edge soak
```

The runtime guardrail line must show V2 submit backpressure enabled, with
bounded pending and oldest-age caps, for example:

```text
v2_submit_backpressure=True pending_cap=5000/<=5000 oldest_age_cap=300.0/<=300.0
```

The prebake line must include both current and next epoch, for example:

```text
epochs=<current>:6220/6220,<next>:6220/6220
```

## 5. Deploy The Open Gate

Only after the strict preflight passes and the operator says go:

```sh
cd deploy/v2-beta-router
npx wrangler deploy --var V2_GATE_MODE:open-v2
```

Immediately confirm that V1 remains retired and V2 reaches origin:

```sh
curl -i https://v2-beta.cathedral.computer/v1/synthetic-boolean/per-miner/challenges?limit=1
curl -i https://v2-beta.cathedral.computer/health/ready
```

## 6. First 15 Minutes

Watch these signals continuously:

- `/v2/verify/metrics` on the private process: `pending_count`,
  `oldest_pending_age`, `processed_last_60s`, `tick_errors_last_60s`,
  `last_batch_ms`.
- Public readiness: `/health/ready`.
- Weight feed freshness and count: `/v1/validator/weights/next`.
- Cloudflare edge status: 5xx rate, origin error rate, and unexpected
  `x-cathedral-v2-beta-origin` values.
- Submit admission: p95 admission latency, 429 rate, and timeout rate.
- Submit backpressure: count of `503 v2_submit_backpressure`, which should be
  temporary and should fall as the verifier drains.
- Postgres health: pool saturation and statement timeouts.

Hold the ramp if verifier pending grows but drains. Roll back if it grows
monotonically, oldest pending age exceeds the threshold, weights go stale, or
the origin starts returning 5xx.

## 7. Rollback

Rollback is the staged worker deploy. It should be tested by dry-run before
launch and kept ready in terminal history:

```sh
cd deploy/v2-beta-router
npx wrangler deploy --var V2_GATE_MODE:staged
```

After rollback, run:

```sh
python deploy/v2-beta-router/relaunch_preflight.py --launch-intent staged --run-edge-soak
```

Expected:

```text
PASS non-canary V2 gate closed
PASS staged edge soak
PRECHECK_OK
```

## No-Go Conditions

- `CATHEDRAL_V2_REAL_FRACTION` is undecided or does not match preflight.
- Current or next epoch prebake is incomplete.
- Capacity probe does not submit, verify, and drain all receipts.
- V2 submit backpressure is disabled, missing from the running process, or not
  returning `503` plus `Retry-After` under a synthetic pending backlog.
- Sustained closed/canary load has not been measured.
- Edge staged soak does not reject every non-canary request at Cloudflare while
  still closed.
- Public process env and env file differ.
- Remote env differs from `deploy/sandbox/env.template.sh`.
- Public verifier is enabled.
- Weight vector is stale or too small.
- Verifier pending queue or oldest pending age grows through the ramp.
- Rollback command has not been dry-run or is not ready.
