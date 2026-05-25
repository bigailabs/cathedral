# AGENTS.md — Cathedral SN39 Orientation

This document exists for engineers and agents who are about to make a change
to the Cathedral validator/publisher and have never read the code before.
The codebase has accumulated subtle traps that are not obvious from any
single file; the goal here is to surface them up-front so you do not burn
a debugging session rediscovering them.

If you only have 60 seconds: skip to **"Two weight paths — read this
first"** and **"Footgun list"** below. The rest is reference material.

## Two weight paths — read this first

The validator has two distinct, mutually exclusive ways of arriving at the
vector it submits to `chain.set_weights`. The selection is a config flag,
but the two paths use **different inputs, different trust roots, and
different burn semantics**.

```
                  ┌──────────────────────────────────────────────────┐
                  │   validator/app.py lifespan: which loop runs?   │
                  │                                                  │
                  │   settings.remote_weight_source.enabled = ?      │
                  └──────────────────────────────────────────────────┘
                              │                              │
                       enabled=false                  enabled=true
                       (stock mainnet.toml)          (opt-in publisher trust)
                              │                              │
                              ▼                              ▼
                  ┌──────────────────────┐      ┌──────────────────────────┐
                  │       PATH A         │      │         PATH B           │
                  │ local aggregation    │      │  signed publisher vector │
                  │                      │      │                          │
                  │ pull_loop.py         │      │ remote_weight_loop.py    │
                  │   pulls eval_runs    │      │   fetches signed vector  │
                  │   from publisher     │      │   from publisher API,    │
                  │                      │      │   verifies Ed25519,      │
                  │ weight_loop.py       │      │   caches in sqlite       │
                  │   aggregates locally │      │                          │
                  │   applies burn       │      │ apply_cached_remote_     │
                  │   normalizes         │      │ vector_once              │
                  │                      │      │   maps hotkeys -> uids,  │
                  │ Burn % = hardcoded   │      │   applies burn from      │
                  │   MAINNET_FORCED_    │      │   vector.burn_snapshot,  │
                  │   BURN_PERCENTAGE    │      │   normalizes             │
                  │   (config.py:17)     │      │                          │
                  │                      │      │ Burn % = publisher-      │
                  └──────────┬───────────┘      │   controlled (signed)    │
                             │                  └────────────┬─────────────┘
                             ▼                               ▼
                  ┌────────────────────────────────────────────────────┐
                  │             chain.set_weights(...)                 │
                  │  Bittensor `set_weights` extrinsic on subnet 39    │
                  └────────────────────────────────────────────────────┘
```

**Stock `config/mainnet.toml` is Path A.** Validators who pulled the repo
and run it as-is are on Path A. Opt-in to Path B by setting
`[remote_weight_source] enabled = true` AND exporting the matching
`CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX` env var.

### How to verify which path is active

Two places to look:

1. **Startup log** — grep stderr for `weight_path_selected`. One JSON
   line emitted during lifespan startup with `path="A"` or `path="B"` and
   the `reason=` field showing the underlying config flag.
2. **`/health` endpoint** — the `weight_path` field on the health snapshot
   reports `"A"` or `"B"`. `curl http://localhost:9333/health | jq .weight_path`.

Both surfaces report the same value. If they disagree, that is a bug in
the lifespan code, not in the config.

## Glossary

A handful of words are overloaded in the codebase. Definitions, in order
of confusion potential:

- **`score`** — publisher-side eval-run result. A single (hotkey, task,
  numeric) row produced by an eval worker against a miner submission.
  Lives in publisher's `eval_runs` table. Pulled into validator's
  `pulled_eval_runs` table by `pull_loop.py`. **Not** the same as a
  weight.

- **`weight`** — validator-side per-uid number in `[0, 1]` that sums to
  ~1 across the metagraph and is submitted on chain. Produced by
  aggregating many `score` rows per hotkey (Path A) or by accepting a
  pre-aggregated signed vector from the publisher (Path B).

- **`extrinsic`** — a Bittensor on-chain transaction. The validator
  submits a `set_weights` extrinsic at the end of each weight loop tick.
  Other extrinsics (commit-reveal, register, etc.) exist but are
  irrelevant here.

- **`set_weights`** — the specific extrinsic for weight submission. Both
  Path A and Path B end here; the diagram above is the funnel.

- **"relay"** — used confusingly in the log event `remote_weight_relayed`.
  Despite the name, this is **not** a relay step — it is the on-chain
  `set_weights` completion event on Path B (read the `status` field for
  the actual outcome). The misleading event name is being phased out;
  new code emits `chain_weights_set_remote` (Path B) and
  `chain_weights_set_local` (Path A) at the equivalent point.
  `remote_weight_relayed` is dual-emitted for one release.

- **"burn UID"** — UID 204 on subnet 39 (the subnet owner). The
  protective emission share gets routed to this UID via `apply_burn` so
  that during periods of unreliable miner signal, emissions go to the
  subnet owner rather than to noise.

- **"backfill"** — `pull_loop`'s first fully-drained catch-up pass over
  the publisher's 7-day eval-run window. `weight_loop` waits on
  `initial_backfill_complete` before the first `set_weights` so a freshly-
  upgraded validator does not publish a vector computed from a half-
  hydrated local DB.

## Footgun list

In rough order of "how often this trips up a new agent":

### 1. `MAINNET_FORCED_BURN_PERCENTAGE` is hardcoded and overrides config

`src/cathedral/config.py:17` defines:

```python
MAINNET_FORCED_BURN_PERCENTAGE = 95.0
```

`_sync_sn39_mainnet_weight_policy` (same file, ~line 354) rewrites the
operator's local toml on startup so `forced_burn_percentage` matches
this constant **whenever `network=finney` AND `netuid=39`**. Editing
`forced_burn_percentage` in `config/mainnet.toml` or any operator-local
override is a no-op on mainnet — the next validator restart clobbers it.

The rewrite now emits a `WARNING config_override_applied …` log line so
the override is at least discoverable in stderr; before that change the
rewrite was completely silent and operators would conclude their config
was broken.

If you genuinely need to change the mainnet burn percentage, you must
edit the constant at `src/cathedral/config.py:17` itself and ship a new
release. There is no operator-side workaround.

### 2. `remote_weight_relayed` IS the chain `set_weights` completion event

This event name reads like the validator received something and is about
to relay it onward. In reality the log line fires **after** a
`chain.set_weights` extrinsic attempt on Path B completes. The "relayed"
verb refers to the validator relaying the publisher's signed vector to
chain — but the chain submission attempt itself is the event. The
`status` field on the event is authoritative: `healthy` = the extrinsic
landed; `disabled` = dry-run completed (weights.disabled=true);
`blocked_by_*` = the chain rejected the attempt.

New canonical name: `chain_weights_set_remote`. Dual-emitted for one
release alongside the legacy event. Future-you should grep for both.

### 3. Path A's 7-day window means changes propagate slowly

`pull_loop.py` walks the publisher's `eval_runs` over a 7-day rolling
window. `weight_loop.py` aggregates `pulled_eval_runs` over the same
7-day window. Publisher-side changes to scoring or weighting **take up
to 7 days to fully propagate** through a Path A validator's local DB.

This is the rationale behind PR1 of the recovery plan: closing the
leaderboard schema leak relies on validators' v1 buckets draining over
the next 7 days. There is no "force-refresh" knob.

### 4. `pull_loop.py:184-194` buckets non-SAT/non-bug-isolation rows as v1

The bucket logic in `latest_pulled_score_per_hotkey`:

```python
if schema_version == 5:
    if task_type not in lane_weights:
        continue
    score_bucket = task_type
elif task_type == "bug_isolation_v1":
    score_bucket = "v3"
else:
    score_bucket = "v1"
```

The `else: score_bucket = "v1"` branch silently buckets anything
non-SAT and non-bug-isolation into the v1 (card) bucket. Combined with
the publisher's leaderboard leak, this is how card-era rows kept getting
weighted as v1 incentive long after SAT was supposed to be the only
active lane. PR4 of the recovery plan turns this into `continue` as
defense-in-depth.

### 5. `[weight_source]` (singular) vs `[remote_weight_source]` (separate)

`config.py` keeps a `WeightSourceConfig` block (`[weight_source]`) for
back-compat with pre-#155 validators. It is **not** the authoritative
remote-mode opt-in — that is `[remote_weight_source].enabled`. If you
are wiring a new remote-weight feature, ignore `[weight_source]`
entirely.

## Where things live

Common questions, mapped to file:line as of the time of this doc.

| Question | File:line |
|---|---|
| How does the validator submit weights on chain (Path A)? | `src/cathedral/validator/weight_loop.py:201` (`chain.set_weights`) |
| How does the validator submit weights on chain (Path B)? | `src/cathedral/validator/remote_weight_loop.py:391` (`chain.set_weights`) |
| Where is the burn percentage applied? | `src/cathedral/chain/weights.py::apply_burn` |
| Where is the hardcoded mainnet burn % defined? | `src/cathedral/config.py:17` |
| Where is the silent toml rewrite? | `src/cathedral/config.py::_sync_sn39_mainnet_weight_policy` |
| Where does `pull_loop` bucket scores by lane? | `src/cathedral/validator/pull_loop.py:184-194` |
| Which loop runs at startup? | `src/cathedral/validator/app.py::build_app` lifespan |
| How does `/health` get its data? | `src/cathedral/validator/health.py::Health.update` (writers) + `validator/app.py::get_health` (reader) |
| Where is the signed-vector schema validated? | `src/cathedral/policy/signing.py::SignedWeightVector.invariant_check` |
| Where is the publisher's `/v1/validator/weights/next` served? | `src/cathedral/publisher/app.py` (Path B vector endpoint) |
| Where is the publisher's leaderboard read? | `src/cathedral/publisher/repository.py::list_eval_runs_recent` |

## Conventions

- **Logging.** All structured events go through `structlog.get_logger(__name__)`.
  Event names are snake_case. Field names match the type they represent;
  reuse names across modules where the semantics are the same (e.g.
  `vector_id`, `policy_version`, `status`).
- **`# noqa`.** Not allowed; if a linter complaint cannot be fixed, the
  code needs a refactor.
- **`# type: ignore`.** Allowed sparingly with a comment explaining why.
- **Tests.** `pytest`, asyncio_mode=auto. New tests live in `tests/`
  mirroring `src/cathedral/`. Do not introduce new fixture frameworks
  without checking `tests/conftest.py` first.
- **Pre-commit hooks.** Never use `--no-verify`. If a hook fails, fix
  the underlying issue and re-stage.

## Pointers to deeper context

- `~/notes/cathedral-recovery-plan.md` — the 4-PR recovery sequence
  (PR1-PR4) plus parallel workstreams. Read this if you are about to
  ship a change that touches weights or the publisher surface area.
- `~/notes/cathedral-disable-eu-ai-act.md` — the full audit of the
  EU-AI-Act card-era surface area and the staged removal plan
  (Workstreams B and C of the recovery plan).
- `docs/` — operator-facing runbooks. Less useful for code work; more
  useful for understanding the live deployment story.
