# Inject lane — continuous additive test-puzzle injection

A second challenge stream that runs **alongside** the native refill loop so we can
measure, on the **live board**, how unpredictable / harder SAT instances affect
solve time and scores — **without risking board availability**.

Native refill is untouched. If this lane is off or stalls, the board still fills
normally. Best of both worlds: native keeps the board full, this lane mixes in
tagged test puzzles.

**Default OFF.** With `CATHEDRAL_INJECT_ENABLED` unset, the publisher is
byte-identical to before — the lane never starts.

## What it does

Each pass (default every 60s), for each configured tier, it retires its own ready
challenges and tops up to a small target of **extra** active challenges that are:

- **Isolated** — minted under a distinct `family_id` (default `gentest`). Native
  refill counts/retires only `synthetic_boolean_v1`, so injected challenges never
  eat native slots and native never retires them. The lane manages its own family.
- **Served identically** — `cnf_source='local'`, so they are served, HMAC-fetch-
  gated, witness-verified on submit, and scored exactly like native challenges:
  one signed solve per `(challenge, hotkey)`, same dedup, same proportional
  scoring.
- **Identifiable** — the `challenge_id` embeds the family label and still parses
  to the right tier, e.g. `sat-t2-random-3sat-gentest-<seed-hex>`. Every solve
  row carries the `challenge_id`, so measurement is a substring match on
  `-gentest-`.

### What makes an injected puzzle different from a native one

- **Seed (headline variable).** Native derives its seed from
  `sha256(utc_hour:tier:seq)` — recomputable offline, so the planted answer is
  predictable and pre-solvable. This lane seeds from `secrets.randbits(63)` (OS
  entropy) — unpredictable, like the standalone generator.
- **Method / shape.** Per-tier configurable; defaults to the **native** method
  and shape for an apples-to-apples *seed-only* comparison. Override to test
  harder instances (e.g. force `ajm`, or raise `n_vars`).

## ⚠️ It moves real income

There is **no zero-value mode**. A solved injected challenge pays real tier weight
(weight derives purely from the tier parsed out of the id). So injecting adds
extra earning opportunities and **will shift live scores** — which is the point of
the experiment, but means you should:

- keep the per-tier target **small**,
- announce it to miners, and
- run it for a bounded window, then turn it off and let the lane's challenges
  retire.

This is a **live-prod change** — enabling it sets env on the live publisher. It is
purely additive and reversible (unset the flag; injected challenges age out), but
treat it as a deliberate, announced experiment.

## Enable (on the publisher service)

```bash
CATHEDRAL_INJECT_ENABLED=1          # master switch (default off)
CATHEDRAL_INJECT_FAMILY=gentest     # isolation family_id
CATHEDRAL_INJECT_TIERS=2            # which tiers to inject (default 1,2)
CATHEDRAL_INJECT_TARGET_T2=5        # active injected challenges to hold per tier
# optional — make injected puzzles differ from native beyond the seed:
# CATHEDRAL_INJECT_METHOD_T2=ajm
# CATHEDRAL_INJECT_NVARS_T2=600  CATHEDRAL_INJECT_NCLAUSES_T2=2556
# CATHEDRAL_INJECT_INTERVAL_SECONDS=60
```

Retirement reuses the native age / distinct-solver thresholds
(`CATHEDRAL_OPEN_WINDOW_RETIRE_AFTER_SECONDS`,
`CATHEDRAL_OPEN_WINDOW_RETIRE_AFTER_DISTINCT_SOLVERS`), scoped to the injected
family.

## Measure

```bash
python measure_inject.py --family gentest --window-hours 24
```

Reports, per tier, for injected vs native over the same window: challenge count,
solved %, time-to-first-solve (min / median / mean), and mean distinct solvers —
the side-by-side answer to "do the unpredictable/harder injected instances solve
slower, and who solves them?"

## Difficulty ladder — paste a rung, measure, climb

The goal is **variance in solve time**. Climb one rung at a time and re-run
`measure_inject.py` after each. Two independent variables, in order:

**Rung 0 — same shape as native (isolate the SEED).** Injected instances match
native exactly except the seed is OS-entropy instead of `sha256(utc_hour…)`. If
native solves suspiciously faster/tighter than `gentest` here, someone is
pre-solving the predictable seed — that's the exploit, surfaced.

```bash
CATHEDRAL_INJECT_ENABLED=1  CATHEDRAL_INJECT_TIERS=2  CATHEDRAL_INJECT_TARGET_T2=5
# method + shape unset → inherit native tier-2 (ajm, 400 vars / 1704 clauses)
```

**Rung 1 — AJM at the phase transition (isolate HARDNESS).** Force the unbiased
`ajm` method at m/n ≈ 4.26. `biased` is easy at any size, so this is the first
rung that can actually move solve time.

```bash
CATHEDRAL_INJECT_METHOD_T2=ajm  CATHEDRAL_INJECT_NVARS_T2=400  CATHEDRAL_INJECT_NCLAUSES_T2=1704
```

**Rung 2+ — scale n, hold the ratio.** Raise `NVARS` keeping `NCLAUSES ≈ 4.26 ×
NVARS`. Hardness climbs steeply with n for `ajm` near threshold.

```bash
CATHEDRAL_INJECT_METHOD_T2=ajm  CATHEDRAL_INJECT_NVARS_T2=800   CATHEDRAL_INJECT_NCLAUSES_T2=3408
# next: 1500 / 6390 … keep going until solve-time spreads
```

**Stop climbing when challenges start getting _zero_ solves** — that means you
overshot the field: variance turns into "too hard for everyone," those slots stop
differentiating, and they still cost real weight on the ones that do solve. Watch
`solved %` in the report; if it falls toward 0 on a rung, step back one.

Caveat: threshold random-3SAT hardness is **not perfectly monotonic** (the
difficulty-ladder open question — "falsified by the P0 spike"). `ajm`-at-larger-n
is the right cheap read on variance, but expect a noisy curve, not a clean line;
the solver-robust ladder (reduced-round preimage / cube-of-real-instance) is a
later piece.

## Verify (no live state)

```bash
python inject_verify.py      # expect: INJECT VERIFY PASS
```

Proves isolation (native counting/retirement never touches injected and
vice-versa), identifiability (family label + correct tier parse), and serve parity
(`cnf_source='local'`, active) on a throwaway store.

## Turn off

Unset `CATHEDRAL_INJECT_ENABLED` and restart. The lane stops minting; existing
injected challenges retire on the normal age / solver-cap schedule. Nothing else
is affected.
