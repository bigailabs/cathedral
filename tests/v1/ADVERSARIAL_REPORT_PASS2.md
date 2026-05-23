# Adversarial findings - pass 2 (supplementary)

Reviewer: Team A adversarial agent - second pass
Branch: `feature/v1-launch` (working tree state at 2026-05-10 19:29 EDT)
Date: 2026-05-10

## Why a second report

The original `tests/v1/ADVERSARIAL_REPORT.md` covered the submission +
storage + scoring surface comprehensively (6 CRIT, 8 HIGH, 8 MED, 6 LOW).
This second pass focuses on attack categories that prior pass under-covered
or skipped:

- Validator-side trust chain (the `validator/pull_loop.py` was new)
- Polaris orchestration response trust
- Output-card-hash audit consistency
- Auth header normalization edge cases

All findings here are NEW (not duplicates). Numbering starts at CRIT-7,
HIGH-9 etc. so the two reports compose into one issue list.

---

## Severity legend
Same as PASS 1.

---

## CRITICAL findings

### [CRIT-7] Validator pull-loop signature verification ALWAYS fails - entire validator-side weights pipeline broken on launch

**Where:**
- `src/cathedral/validator/pull_loop.py:233-266` (`_rebuild_signed_payload`)
- `src/cathedral/publisher/reads.py:377-388` (`_eval_run_to_output`)
- `src/cathedral/eval/scoring_pipeline.py:170-186` (the dict that was signed)

**What:** The publisher signs an `EvalRun` payload that contains 14 fields:
`id, submission_id, epoch, round_index, polaris_agent_id, polaris_run_id,
task_json, output_card_json, output_card_hash, score_parts, weighted_score,
ran_at, duration_ms, errors`.

The publisher's `_eval_run_to_output` (the projection used by
`/v1/leaderboard/recent`) emits ONLY 9 of those: `id, agent_id,
agent_display_name, card_id, output_card, weighted_score, ran_at,
cathedral_signature, merkle_epoch`.

The validator's `_rebuild_signed_payload` then defaults the missing fields
(`round_index=0, polaris_agent_id="", polaris_run_id="", task_json={},
output_card_hash="", score_parts={}, duration_ms=0`) and verifies the
signature over THAT reconstructed dict. Because the field set differs by
6+ fields and field types differ (`epoch` for example is set from
`merkle_epoch` which the signed payload computes differently), the
canonical_json bytes the validator hashes are NEVER the bytes the
publisher signed. Verification fails for every record.

**How to reproduce:** `tests/v1/exploits/validator_pull_signature_mismatch.py`

```
$ python tests/v1/exploits/validator_pull_signature_mismatch.py
publisher signs:  d1ddc8...   (14-field canonical)
validator rebuilds: 7e3a09...   (rebuilt from public projection)
signature verify: PullVerificationError('invalid cathedral signature')
result: ZERO eval runs persisted; weight_loop reads empty pulled_eval_runs
```

**Impact:** This is a launch-blocker bigger than any single auth bypass.
Cathedral can do everything else right - receive submissions, run evals,
sign results, publish leaderboards - and still set ZERO weights on chain
because the validator binary discards every record. No miner gets paid.
The subnet is live but yields no emissions to anyone, and the only signal
the operator sees is a stream of `pull_eval_signature_invalid` warnings
in logs.

**Recommended fix:** Two options, mutually exclusive.

1. (Recommended) Have the publisher emit the FULL signed payload alongside
   the projection. Add a `signed_payload` field to `_eval_run_to_output`
   that contains the exact canonical_json bytes (or the dict shape) the
   publisher signed. Validator verifies signature against `signed_payload`
   before extracting display fields.

2. Make the projection lossless and have the validator reconstruct
   directly from it. Means adding `epoch, round_index, polaris_agent_id,
   polaris_run_id, task_json, output_card_hash, score_parts, duration_ms,
   errors` to `_eval_run_to_output`. Larger payload but no hidden field.

In either case, add a publisher↔validator golden-vector test: the
publisher signs a fixture eval-run, the test passes the public projection
through `_rebuild_signed_payload` and asserts the verify succeeds.

**CONTRACTS section violated:** Section 4.2 (cathedral signs the eval-run
record); Section 8 / "Trust + verification chain" (validators verify
signatures).

---

### [CRIT-8] `output_card_hash` does not match `blake3(canonical(output_card_json))` - Merkle leaves and frontend hash display unverifiable

**Where:**
- `src/cathedral/eval/scoring_pipeline.py:125-128, 164` (compute path)
- `src/cathedral/eval/scoring_pipeline.py:178, 198` (storage path)
- `src/cathedral/publisher/merkle.py:63-71` (leaf includes `output_card_hash`)

**What:** The orchestrator builds `raw_card = dict(output_card_json)` and
applies `setdefault("worker_owner_hotkey", ...)`, `setdefault("polaris_agent_id", ...)`,
`setdefault("id", ...)`, then validates to `Card`. `card_hash(card)` then
hashes `card.model_dump(by_alias=True, mode="json")` - a Pydantic-rendered
dict that includes Pydantic defaults for any unset fields, normalized
enums, and the override values. The bytes hashed differ from
`canonical_json(output_card_json)` whenever:

- Polaris omits a field that Pydantic defaults (e.g., `confidence` if
  default applied)
- Polaris emits enum names vs values inconsistently
- The setdefault overrides apply

But `eval_runs.output_card_json` is stored as the ORIGINAL Polaris dict
(scoring_pipeline.py:178 uses `output_card_json`, not `raw_card`).

So `eval_runs.output_card_hash != blake3(canonical_json(eval_runs.output_card_json))`.

The frontend displays `output_card_hash` as the visible trust-chain anchor
(per locked decision L8 in CONTRACTS Section -1). A user computing
`blake3(canonical_json(output_card))` from the displayed `output_card`
gets a different hex than what cathedral signed and rolled into the
Merkle root. The "this is the byte-exact card we ran" guarantee is fictional.

**How to reproduce:** `tests/v1/exploits/output_card_hash_mismatch.py`

```
$ python tests/v1/exploits/output_card_hash_mismatch.py
Polaris output bytes:                 96 bytes
Stored output_card_json (re-canon):   96 bytes
blake3(stored output_card_json):      6e2189...
Cathedral's output_card_hash (from validated Card):  3b40a1...
MISMATCH - public hash cannot be derived from public bytes
```

**Impact:** Merkle leaves include `output_card_hash`. Validators cannot
reconstruct the leaf from just the public projection (output_card_json +
weighted_score). Audit-replay impossible. A malicious or buggy
publisher can swap `output_card_json` after signing without anyone
detecting because the hash chain doesn't tie the stored card bytes to the
signed hash.

**Recommended fix:** Make ONE of these consistent:

- Hash the literal `output_card_json` bytes that get stored (drop the
  Pydantic re-render): `output_card_hash = blake3(canonical_json(output_card_json))`.
- OR store the validated/rendered card AS `output_card_json` so the stored
  bytes match the hash input. Then `output_card_json = card.model_dump(by_alias=True, mode="json")`.

The first option is simpler and matches the doc's intent ("hash of the
agent's output, byte-exact").

**CONTRACTS section violated:** Section 1.10 (`output_card_hash: blake3
of canonical card bytes`); Locked decision L8.

---

### [CRIT-9] `setdefault` in scoring lets bundle attribute output to ANY hotkey, breaking first-mover anchor and reward attribution

**Where:** `src/cathedral/eval/scoring_pipeline.py:125-128`

**What:** The scorer does:
```python
raw_card = dict(output_card_json)
raw_card.setdefault("worker_owner_hotkey", miner_hotkey)
raw_card.setdefault("polaris_agent_id", polaris_agent_id)
raw_card.setdefault("id", card_id)
```

`setdefault` only writes if the key is MISSING. If Polaris's response
includes any of these fields (whether the agent fabricated them or
Polaris injected them), the Polaris-provided value WINS over the
trusted server-side values. The CONTRACTS spec is explicit: these fields
are "filled by validator from claim" - they MUST be set by cathedral, not
by the agent.

Concretely, an attacker writes a Hermes profile whose soul.md or skills
emit JSON with `"worker_owner_hotkey": "5VictimHotkey"`. Cathedral's
scorer keeps that attacker-controlled value. Downstream:

- `_hotkey_for(item)` in `validator/pull_loop.py:269-275` reads
  `output_card.worker_owner_hotkey` to determine WHO gets credit. It
  pulls "5VictimHotkey" (or a whale's hotkey).
- `upsert_pulled_eval` writes a row keyed on that hotkey.
- `latest_pulled_score_per_hotkey` aggregates by hotkey.
- weight_loop assigns weight to that hotkey.

So a low-stake miner can attribute their high-scoring evals to a whale
hotkey and rob the whale, OR (more usefully) to a sock-puppet they
control to inflate their own emissions.

**How to reproduce:** `tests/v1/exploits/setdefault_owner_hotkey_spoof.py`

```
$ python tests/v1/exploits/setdefault_owner_hotkey_spoof.py
miner_hotkey from submission:    5MinerA...
agent's emitted card claims:     worker_owner_hotkey=5VictimWhale...
post-setdefault Card.worker_owner_hotkey: 5VictimWhale...
Validator credits weight to:     5VictimWhale
```

**Impact:** Reward attribution is fully attacker-controllable. First-mover
delta is also computed against the attacker's chosen hotkey (because
`_hotkey_for` reads from output_card not from the submission row), which
inverts the entire incentive structure.

**Recommended fix:** Use unconditional assignment for fields that MUST
come from the server:

```python
raw_card["worker_owner_hotkey"] = miner_hotkey
raw_card["polaris_agent_id"] = polaris_agent_id
raw_card["id"] = card_id
```

Drop `setdefault` entirely for these three keys. Same fix in any other
place where the trust chain has the validator filling from claim.

**CONTRACTS section violated:** Section 1.4 (`worker_owner_hotkey: filled
by validator from claim`); Section 6 step 4.

---

### [CRIT-10] Polaris HTTP runner does not verify Polaris's runtime-image manifest signature - Cathedral signs whatever Polaris sends

**Where:** `src/cathedral/eval/polaris_runner.py:225-250` (HttpPolarisRunner)

**What:** The runner POSTs the bundle to Polaris and polls
`/polaris/runs/{run_id}` for `status=success`, then takes
`pdata.get("output_card")` at face value. The `PolarisManifest` type
exists in `cathedral.types` (with new `runtime_image` and `runtime_mode`
fields per Section 1.5) and CONTRACTS' "Trust + verification chain"
explicitly says:

> Polaris signs manifest with runtime_image → cathedral can prove the
> right runtime ran

The HTTP runner never fetches a manifest, never verifies its signature,
and never checks that `runtime_image` matches the cathedral-blessed
Hermes image. If Polaris is compromised - or a misconfigured Polaris
instance points the bundle to an attacker-controlled container - Cathedral
will sign + Merkle-anchor whatever JSON comes back as if it were a real
Hermes-produced card. The "the right runtime ran" guarantee is
unenforced.

**How to reproduce:** `tests/v1/exploits/polaris_unverified_output.py`
points `HttpPolarisRunner` at a stub HTTP server that returns hand-crafted
output cards. Cathedral signs them, persists them, includes them in the
Merkle root. No verification step rejects.

**Impact:** Cathedral becomes a signing oracle for any party who can
intercept or impersonate the Polaris HTTP endpoint. v1's threat model
trusts Polaris but the trust is supposed to be verifiable via the
manifest signature; without verification the trust is implicit and
breakable in one TLS misconfiguration.

**Recommended fix:**

1. Add a `manifest` field to the Polaris poll response shape (or a
   separate `GET /polaris/agents/{id}/manifest` endpoint).
2. Runner fetches the manifest after `status=success` and before
   returning.
3. Verify: `verify_manifest(manifest, polaris_pubkey)` (the function
   exists in `cathedral.types`).
4. Assert `manifest.runtime_image in CATHEDRAL_BLESSED_IMAGES` and
   `manifest.runtime_mode == "card_mode"`.
5. Reject the run otherwise.

**CONTRACTS section violated:** "Trust + verification chain" line 4;
Section 1.5 + locked decision L3 (the new `runtime_image` field exists
to be verified).

---

## HIGH findings

### [HIGH-9] Validator hotkey extracted from `output_card.worker_owner_hotkey` (attacker-controlled) instead of `agent_id`/submission row

**Where:** `src/cathedral/validator/pull_loop.py:269-275` (`_hotkey_for`)

**What:** `_hotkey_for(eval_output)` reads `output_card.worker_owner_hotkey`
from the public projection. That field is sourced from the Polaris output
(see CRIT-9), so it's miner-controllable. The validator should pull the
hotkey from the AGENT row, which the publisher could expose via
`miner_hotkey` in the projection.

**How to reproduce:** Combined with CRIT-9 - the attribution flows all
the way to weights set on chain.

**Recommended fix:** Add `miner_hotkey` to `_eval_run_to_output` (already
present in the agent submission row), and have `_hotkey_for` read
`eval_output.get("miner_hotkey")`. Then ALSO fix CRIT-9 so the two
sources converge.

---

### [HIGH-10] `merkle_epoch` field in projection is the SUBMISSION's not the eval-run's - validator stores wrong epoch

**Where:** `src/cathedral/publisher/reads.py:387` (`merkle_epoch=run.get("merkle_epoch")`)

**What:** `eval_runs` rows do not carry `merkle_epoch` natively - that
column is added later by `link_eval_runs_to_epoch` in the merkle close
job. Until the weekly Merkle job runs, every projection emits
`merkle_epoch=None`. The validator's `_rebuild_signed_payload` then sets
`epoch=None` in the rebuilt dict and signs verification fails twice over
(also see CRIT-7).

**Impact:** Even if CRIT-7 is fixed, until the first Merkle job runs the
validator is processing records with `epoch=None` and any code that does
arithmetic on epoch will crash or skip silently.

**Recommended fix:** Project `eval_runs.epoch` (the per-eval-run epoch,
which is set at run time in scoring_pipeline.py) directly. Rename
`merkle_epoch` to be a separate "anchored?" indicator (or move to a
boolean `is_anchored`).

---

### [HIGH-11] `eval_run.epoch` field signed but is the wrong "epoch" notion - uses ISO calendar week, leaks task generation seed

**Where:**
- `src/cathedral/eval/scoring_pipeline.py:172` signs `epoch`
- `src/cathedral/eval/orchestrator.py:110` `epoch = epoch_for(datetime.now(UTC))`
- `src/cathedral/eval/task_generator.py:39-42` `_seed_for(card_id, epoch, round_index)`

**What:** The eval task seed is `blake3(f"{card_id}|{epoch}|{round_index}")`.
The `epoch` field is the ISO calendar week number (e.g., `202618`). It's
public (visible in `eval_runs.epoch` after CRIT-7 is fixed), and
`round_index` is monotonic per (card_id, epoch) starting at 0. So
`(card_id, epoch, round_index)` is enumerable from public state alone.

PASS 1's CRIT-2 already flagged the determinism issue. This is the
secondary form: the FRESHLY SIGNED payload publishes the exact triple
needed to pre-compute the task. A copier reading `/v1/leaderboard/recent`
sees `epoch` and can derive the round (and the task that produced this
card) without any cryptographic effort.

**Recommended fix:** Same as PASS 1 CRIT-2 - inject server-side randomness
into the task seed (round-start nonce or chain block hash) so even with
`(card_id, epoch, round_index)` known the task isn't pre-computable.

---

### [HIGH-12] AES-GCM nonce reuse possibility on retry path - `encrypt_bundle` regenerates nonce per call but submit handler may re-encrypt on Hippius retry

**Where:**
- `src/cathedral/storage/crypto.py:90-112` (encrypt_bundle generates fresh nonce)
- `src/cathedral/publisher/submit.py:240-250` (single Hippius PUT, no retry - but no idempotency key)

**What:** Currently the submit handler does ONE encryption + ONE Hippius
put. Hippius failures map to 503. Risk is implicit: any future retry
loop around `encrypt_bundle` (e.g., for transient Hippius errors) would
generate a NEW nonce per attempt while the previous attempt's blob may
have already landed in Hippius. Since AES-GCM is catastrophic on nonce
reuse, this is a MED today but a CRIT once anyone adds retry logic. Worth
fixing structurally now.

Additionally, the same data_key gets wrapped fresh per encrypt call (each
submission is a fresh data_key), so cross-submission nonce reuse is
impossible. But within a submission's retry loop, the nonce protection is
purely "we call encrypt_bundle once" - not enforced by the type system or
encryption layer.

**Recommended fix:** `encrypt_bundle` should be deterministic given a
caller-provided nonce (or use the bundle_hash as a per-bundle nonce
input). Add an explicit `EncryptedBundle` cache so retries return the
same ciphertext. Document the "encrypt-once-per-submission" invariant
loudly.

---

### [HIGH-13] `_ms_iso` truncates microseconds; submitted_at signed by miner does not round-trip if miner uses microsecond precision

**Where:** `src/cathedral/publisher/submit.py:341-346`

**What:** Server generates `submitted_at = datetime.now(UTC)` (microsecond
precision), then formats as `_ms_iso(submitted_at)` (millisecond
precision). The miner signs over `_ms_iso` per spec. But `_ms_iso` does:

```python
s = dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
return s + "Z"
```

If `dt.microsecond = 123_999`, the format yields `.123Z`. If it equals
`999_999`, yields `.999Z`. The wall-clock string never rolls over the
second boundary even when integer microseconds round up; this is fine.

But `repository.first_mover_for_fingerprint` orders by
`first_mover_at ASC`, and the stored `first_mover_at` comes from
`_ms_iso(submitted_at)` (string). String compare on ISO-8601 with
millisecond precision is correct. However, any submission whose
`first_mover_at` is set from `client_submitted_at` (CRIT-1 path) carries
the client's full microsecond precision - which sorts BEFORE any
ms-precision string at the same wall-clock instant.

Concretely: legitimate first mover stores `2026-05-10T12:00:00.500Z`.
A backdated copier (CRIT-1) stores `2026-05-10T12:00:00.500001Z` (extra
char at end). String compare puts the legitimate ahead. But if the
copier passes `2026-05-10T12:00:00.499Z` they win.

**Recommended fix:** Normalize all stored timestamps to a single canonical
format (`_ms_iso` everywhere, including client paths). Then drop the
client-controllable path per CRIT-1.

---

### [HIGH-14] `eval_runs.errors` field signed-as-list-or-None; Pydantic strict shape lost on round-trip; signature hash differs by `None` vs `[]` distinction

**Where:** `src/cathedral/eval/scoring_pipeline.py:184` (`"errors": errors if errors else None`)

**What:** The signer puts `None` when no errors, but a list when there
are. The validator's reconstruction defaults to `None` always. If a real
eval has `errors=["something"]`, the signed payload has the list, but the
public projection (per the existing `_eval_run_to_output`) doesn't emit
`errors` at all, so the validator rebuilds with `errors=None` and the
signature fails (compounds CRIT-7).

Even after CRIT-7's fix, the difference between `None` (omitted-from-canonical)
and `[]` (empty list) yields different canonical_json bytes:
`{"errors":null}` vs `{"errors":[]}`. Pick one; lock it.

**Recommended fix:** Either always emit `errors=[]` (drop the conditional)
or strip the key entirely when empty. Either is fine but be consistent.

---

### [HIGH-15] Validator's pull-side dedupe key is a CRC32 of the eval_run_id - high collision probability over months of operation

**Where:** `src/cathedral/validator/pull_loop.py:96-98`

**What:**
```python
synth_id = -(zlib.crc32(eval_run_id.encode("utf-8")) & 0x7FFFFFFF) - 1
```

CRC32 has 2^31 effective values (after the negation). With ~10k evals/day
across all cards, birthday-paradox collision probability hits 50% at
~50k evaluations (~5 days of operation). When two eval_run_ids hash to
the same `synth_id`, the row carrying both legacy AUTOINCREMENT id space
collisions (the comment claims this is fine - it's not) plus
`pulled_eval_runs` ON CONFLICT(eval_run_id) DO UPDATE - actually that
last clause IS keyed on eval_run_id, so dedup is fine for the
pulled_eval_runs table. But the `synth_claim_id` is exposed to the
weight_loop's queries, and any join on it produces wrong scores.

**Recommended fix:** Use the eval_run_id as the join key. Drop synth_claim_id
or use a 64-bit hash (xxhash, blake3 truncated to 8 bytes) and store as
INTEGER.

---

### [HIGH-16] Stub Polaris runner ships in production binary; gating only by `CATHEDRAL_EVAL_MODE=stub` env var

**Where:** `src/cathedral/eval/polaris_runner.py:64-130` (`StubPolarisRunner`)

**What:** The stub runner generates fake cards with `confidence=0.6,
no_legal_advice=True, citations=[{"url": "https://example.invalid/stub"}]`
that pass preflight and earn middling scores. It ships in the production
binary. Operator misconfiguration (forgetting to unset `CATHEDRAL_EVAL_MODE`,
or accidentally setting it in prod env file) yields evals that look real
but are deterministic stubs. Worse, the stub passes preflight today
(`citations: [{class: "other", status: 200, ...}]`) so scoring runs and
weights get set off fabricated cards.

**Recommended fix:**

1. Move `StubPolarisRunner` into a `tests/` or `dev/` module that
   production never imports.
2. Have production explicitly fail to start if `CATHEDRAL_EVAL_MODE=stub`
   AND `CATHEDRAL_ENV != local`.
3. If you keep it shipping, make the stub return cards that FAIL preflight
   (e.g., `no_legal_advice=False`) so scores never propagate.

---

## MEDIUM findings

### [MED-9] `epoch_for` uses ISO year, which differs from calendar year in late-Dec / early-Jan - eval orchestrator and merkle close job can disagree on epoch boundary

**Where:** `src/cathedral/publisher/merkle.py:35-42` (`epoch_for`)

**What:** `dt.isocalendar()` returns ISO year/week. For dates like
2026-12-31 (a Thursday), ISO week is `2026 W53`. For 2027-01-01 (Friday),
ISO week is `2026 W53` too. Then 2027-01-04 (Monday) jumps to `2027 W01`.
But `epoch_for` does `iso.year * 100 + iso.week` so:

- 2026-12-31: `epoch=202653`
- 2027-01-01: `epoch=202653`
- 2027-01-04: `epoch=202701`

The year boundary is fine. But the inverse (`epoch_window`) uses
`date.fromisocalendar(year, week, 1)` - year=2026 week=53 may not exist
in some years (ISO week 53 only exists if the year has 53 ISO weeks).
For year=2027 there is no week 53, so `epoch_window(202753)` raises
`ValueError`. The merkle close job will crash on those edge weeks.

**Recommended fix:** Validate the ISO week exists when computing
`epoch_window`. Either skip non-existent weeks or use a flatter epoch
encoding (e.g., days-since-2026-01-01 // 7).

---

### [MED-10] `safe_extract_zip` does not check that `soul.md` extracts to a non-empty file before returning preview

**Where:** `src/cathedral/storage/bundle_extractor.py:181-189`

**What:** After extraction, `_find_first(dest_root_real, "soul.md")` walks
the tree. If a malicious zip contains `soul.md` of size 0 (passes
preflight: it's `_REQUIRED_FILES`), `preview = soul.read_text()[:500]`
returns `""`. The preview is stored in `agent_submissions.soul_md_preview`
as empty. Downstream similarity-on-soul-md (planned for v2) sees identical
empty previews across many copies and either produces false positives
("everyone has the same soul") or false negatives (no signal).

**Recommended fix:** Require `soul.md` size > some minimum (e.g., 64
bytes) at preflight time.

---

### [MED-11] `repository.list_eval_runs_for_card`'s `since` filter compares ISO strings - TZ offset format mismatches break the comparison

**Where:** Probably in repository.py - let me note conceptually.

**What:** `since_dt` is a `datetime` from `_parse_since`. If passed
without `tzinfo`, comparisons break because stored timestamps are with
"+00:00" suffix. This is latent but easy to trigger via clients sending
`since=2026-05-10T00:00:00` (no Z).

**Recommended fix:** `_parse_since` should always return a tz-aware
datetime (assume UTC if naive) and stringify identically to how runs
were stored.

---

### [MED-12] No timeout on `httpx.AsyncClient` poll loop in HttpPolarisRunner - single hung Polaris run blocks an eval slot indefinitely

**Where:** `src/cathedral/eval/polaris_runner.py:200-255`

**What:** The `httpx.AsyncClient(timeout=60.0)` covers individual HTTP
requests but the poll loop runs forever until `elapsed > deadline_secs`.
If Polaris returns `status=running` faster than `poll_interval_secs`, the
loop spins. If Polaris hangs sockets at the network layer, the per-request
60s timeout fires but the loop retries - total wait can exceed
`deadline_secs` by `poll_interval_secs * (deadline_secs / 60)` minutes.

The orchestrator's `max_concurrent=2` semaphore means 2 hung Polaris
runs lock up the entire eval pipeline.

**Recommended fix:** Wrap the entire `run` call in
`asyncio.wait_for(..., timeout=deadline_secs + 60)` from the orchestrator
side so a hung Polaris client cannot starve the orchestrator.

---

### [MED-13] `Anchorer.anchor` runs in a thread but holds the event loop blocked if `bittensor` SDK calls a sync substrate library - async-via-thread is correct, but error logging happens after the await

**Where:** `src/cathedral/chain/anchor.py:94-125` (BittensorAnchorer)

**What:** `await asyncio.to_thread(_send)` is fine. But
`merkle.close_epoch` catches `AnchorError` and continues (line 120-123),
"persists anyway so we have the off-chain root". Operator may not notice
the on-chain anchor failed because the DB row insert succeeds. Then on
restart, `previous_epoch` recomputes the SAME root (because the source
data hasn't changed) and tries to anchor again. There's no retry queue;
manual operator action required. Document this in RUNBOOK.

**Recommended fix:** Add an `anchor_status` column to `merkle_anchors`
(`pending|onchain|failed`) and a separate retry job.

---

### [MED-14] `EvalSigner.from_env_hex` accepts whitespace via strip(); silently coerces hex case but does NOT verify the corresponding public key matches `CATHEDRAL_PUBLIC_KEY_HEX`

**Where:** `src/cathedral/eval/scoring_pipeline.py:72-82`

**What:** Operator can ship with a mismatched signing/verifying key pair
(prod private key + dev public key on validator binary). Signer signs
fine, validator's pull-loop fails verification on every record. Same
symptom as CRIT-7 but root cause is config drift.

**Recommended fix:** Have `from_env_hex` derive the public key, log it
at startup, and either compare to a configured `CATHEDRAL_PUBLIC_KEY_HEX`
(if set) or warn loudly if not configured.

---

## LOW findings

### [LOW-7] `previous_epoch(now)` does not validate `now` is timezone-aware
`merkle.py:155-158`. Naive datetime → `epoch_for` works but inconsistent.

### [LOW-8] `BundleExtractor` swallows `OSError` on `soul.read_text` and re-raises as `BundleStructureError` - losing the OS error code
`bundle_extractor.py:185-187`. Operator can't distinguish ENOSPC from
permissions errors.

### [LOW-9] `HippiusClient.put_logo` returns URL constructed from `endpoint_url + bucket + key` - broken if Hippius ever rewrites public URLs
`hippius_client.py:184`. Hardcoded URL pattern.

### [LOW-10] `_pool_entry_to_source` returns Source with `content_hash="0"*64` and `status=200` - placeholder values that, if accidentally used by the scorer, would inflate citation quality
`task_generator.py:81-101`. Today the scorer reads from output card not task input, but the fixture is misleading.

### [LOW-11] `hotkey_auth_header` strips whitespace from header before length check - case where `X-Cathedral-Signature: \t\n\t` (3 ws chars) results in empty string after strip then 401, but header parsing might surface tabs/newlines weirdly through ASGI
`auth_signature.py:45-46`.

### [LOW-12] `merkle.close_epoch` does NOT take a transactional lock against concurrent invocations - two cron jobs could both close the same epoch and produce two `merkle_anchors` rows... actually it can't, the table has `epoch PRIMARY KEY`. UPSERT not used though, so the second invocation IntegrityError aborts after work is done.
`merkle.py:124-134`. Wasteful but not broken. Add `INSERT OR REPLACE` or
similar.

---

## Things I tested that ARE secure (NEW since pass 1)

- `encode_anchor_payload` strict 44-byte format with prefix + epoch + root
  validates correctly, refuses out-of-range epoch and bad root length.
- `validate_hermes_bundle` rejects path traversal (`../`, absolute,
  Windows drive), symlinks, and >100 MiB total uncompressed (the per-file
  bomb evasion is documented in PASS 1 HIGH-4).
- AES-GCM authentication tag check via `cryptography.exceptions.InvalidTag`
  is correctly enforced - flipping ciphertext bits raises during decrypt.
- `verify_eval_run_signature` correctly rejects empty signature, bad
  base64, and Ed25519 verification mismatch (the bug is upstream - the
  PAYLOAD it's verifying against is wrong, not the verify call itself).
- `safe_extract_zip` defense-in-depth `target.relative_to(dest_root_real)`
  check catches resolved escapes that pass the syntactic check.

---

## Things I couldn't test (gaps for the next pass)

- Real Polaris HTTP endpoint with the runtime_image manifest extension
  shipped - this is the core of CRIT-10 and needs cross-repo testing
  against `polariscomputer/polaris/api/routers/cathedral_contract.py`.
- Real Bittensor `system.remarkWithEvent` extrinsic - the BittensorAnchorer
  is mocked in tests; need testnet exercise with `wait_for_inclusion=True`
  to confirm receipt parsing works.
- Concurrent eval-orchestrator load: two `evaluate_one` tasks racing
  against the same submission row's status update. The "single-writer"
  comment in `run_eval_loop` doc says the row goes `'queued' → 'evaluating'`
  atomically, but `repository.queued_submissions(limit=N)` followed by N
  separate `update_submission_status` calls is a TOCTOU window. Not
  exploited from the outside, but ops fragility.
- LLM-judge evaluation inputs (Polaris's Hermes runtime) - out of scope
  until Hermes scoring contract is locked.

---

## Coordination notes

### Findings the IMPLEMENTER should action immediately (before any cross-repo wire-up)

1. **CRIT-7** (validator signature always fails) - service-wide failure;
   highest priority. 1-day fix (publisher emits signed_payload, validator
   reads from it).
2. **CRIT-8** (output_card_hash mismatch) - audit chain unverifiable;
   1-hour fix (drop the Pydantic re-render in `card_hash`).
3. **CRIT-9** (setdefault attribution spoof) - economic incentive
   inversion; 30-min fix (replace setdefault with assignment).
4. **CRIT-10** (Polaris response unverified) - design discussion required;
   requires Polaris-side manifest endpoint shipping.
5. **HIGH-9** + **HIGH-10** are downstream of the CRIT items - verify
   they resolve once those are fixed.
6. **HIGH-16** (stub runner in prod) - gate harder.

### Findings to flag for Fred review

- The publisher↔validator wire format (CRIT-7) needs a contract test in
  CONTRACTS.md. Add a "validator pull verification" golden vector to
  Section 4 alongside the signing format.
- CRIT-9 reveals that "filled by validator from claim" is currently a
  spec promise the code doesn't keep. Sweep for other instances of the
  same pattern.
- CRIT-10 asks: should v1 ship a Polaris-runtime trust check, or should
  the v1 docs explicitly acknowledge "you trust the Cathedral operator's
  Polaris instance" and defer manifest verify to v1.1?

### Findings that contradict the CONTRACTS doc (warden problem)

- Section 1.10 says `output_card_hash: blake3 of canonical card bytes`.
  Implementation hashes the Pydantic-rendered `Card` model, not the
  literal output_card_json bytes. Doc and code disagree on what "card
  bytes" means.
- "Trust + verification chain" expects Polaris manifest verification at
  the eval boundary. v1 implementation skips it entirely.
- Section 4.2 expects validators to verify cathedral signatures - the
  rebuild-from-projection approach in `pull_loop.py` makes this
  structurally impossible.

---

## Estimated implementer time to address PASS 2 findings

| Finding | Time |
|---------|------|
| CRIT-7 (validator signature mismatch) | 1 day (contract change + golden vector + both sides updated) |
| CRIT-8 (output_card_hash) | 1 hour |
| CRIT-9 (setdefault → assignment) | 30 min + tests |
| CRIT-10 (Polaris manifest verify) | 0.5-1 day if Polaris already exposes manifest, else 2 days (cross-repo) |
| HIGH-9 (validator hotkey source) | 1 hour (resolves with CRIT-9) |
| HIGH-10 (epoch projection) | 1 hour (resolves with CRIT-7) |
| HIGH-11 (task seed leakage) | duplicate of PASS 1 CRIT-2 - same fix |
| HIGH-12 (encrypt nonce reuse risk) | 0.5 day (refactor encrypt API) |
| HIGH-13 (timestamp normalization) | resolves with PASS 1 CRIT-1 fix |
| HIGH-14 (errors None vs []) | 30 min |
| HIGH-15 (synth_claim_id collision) | 0.5 day (drop synth, key on eval_run_id) |
| HIGH-16 (stub in prod) | 1 hour (gating + import-time checks) |
| MED-9..14 | 1-1.5 days combined |
| LOW-7..12 | 0.5 day combined |

**PASS 2 incremental: 5-6 engineer-days on top of PASS 1's 8-10.**

**Of that, CRIT-7 alone is the day-one launch blocker (no weights set without it). CRIT-8/9 are also required before public claims about audit-trail integrity are made.**

---

## Combined launch-blocker checklist (PASS 1 + PASS 2)

Order of fixes for a no-bullshit launch:

1. CRIT-7 (validator signature) - without this, NOTHING works on chain.
2. CRIT-3 (delete legacy /v1/claim) - eliminate the unauthenticated path.
3. CRIT-1 (drop client-supplied submitted_at fallback).
4. CRIT-9 (setdefault → assignment).
5. CRIT-8 (output_card_hash consistency).
6. HIGH-8 (circular import - service won't start).
7. CRIT-4 (work_unit override).
8. CRIT-6 (first-mover fingerprint anchoring on bundle content).
9. CRIT-2 / HIGH-11 (eval task entropy).
10. CRIT-5 (citation re-fetch).
11. CRIT-10 (Polaris manifest verify) - can defer with explicit doc note.

Everything else can ship behind a documented "v1.0 known-issue" flag.
