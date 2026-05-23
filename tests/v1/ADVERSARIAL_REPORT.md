# Adversarial findings - cathedral v1

Reviewer: Team A adversarial agent
Branch: `feature/v1-launch` (committed: 7 commits including #7 inline cards;
uncommitted in working tree: full v1 launch modules under
`src/cathedral/{auth,storage,publisher,eval}/` plus `chain/anchor.py` and
`v1_types.py` - added by the implementer in parallel during my review.)
Date: 2026-05-10

## Scope and framing

I reviewed:
- The original `feature/v1-launch` branch (7 commits, last sha 565052c) covering
  `src/cathedral/{validator,evidence,cards,chain,miner,types,config}.py`.
- The implementer's uncommitted work in tree:
  `src/cathedral/auth/hotkey_signature.py`,
  `src/cathedral/storage/{crypto,hippius_client,bundle_extractor}.py`,
  `src/cathedral/publisher/{submit,auth_signature,similarity,merkle,reads,repository}.py`,
  `src/cathedral/eval/{task_generator,polaris_runner,scoring_pipeline,orchestrator}.py`,
  `src/cathedral/chain/anchor.py`,
  `src/cathedral/v1_types.py`,
  diff to `src/cathedral/validator/db.py` adding card_definitions /
  agent_submissions / eval_runs / merkle_anchors / eval_run_to_epoch.
- historical local design doc `CONTRACTS.md`
- historical local design doc `ARCHITECTURE_V1.md`

The validator-side legacy module (Polaris claim verification with inline
cards) and the new v1 publisher/eval/storage are TWO different code
paths that will likely run side-by-side. I attacked both. Findings are
split by which code path they hit.

## Severity legend
- **CRITICAL**: launch-blocking. Direct path to fund loss, identity theft, IP leak, or total subnet failure.
- **HIGH**: must fix before public launch. Breaks the trust/incentive model even if not catastrophic.
- **MEDIUM**: should fix in v1; can defer to v1.1 with a documented mitigation.
- **LOW**: nice to fix; no immediate exploit path.

## CRITICAL findings

### [CRIT-1] `submit_agent` accepts client-supplied `submitted_at` via form fallback → backdated first-mover steal
**Where:** `src/cathedral/publisher/submit.py:148-189`
**What:** The submission handler computes `submitted_at = datetime.now(UTC)` server-side, but if the hotkey signature does NOT verify against that value it falls back to verifying against a `submitted_at` form-field the client supplied. If THAT verifies, the client value wins and is recorded as `agent_submissions.submitted_at` AND propagates to `first_mover_at`. The client controls the timestamp.
**How to reproduce:** `tests/v1/exploits/replay_attack_submission.py` (analysis); `datetime.fromisoformat` parses arbitrary backdated and far-future values without complaint. Then craft any miner-side script that:
1. Generates a signature over `(bundle_hash, card_id, hotkey, submitted_at='2024-01-01T00:00:00.000Z')`.
2. POSTs the bundle, signature, and `submitted_at` form field.
3. Server-side `now()` doesn't match → fallback path → uses client value → row created with `first_mover_at = '2024-01-01...'`.
4. `repository.first_mover_for_fingerprint` returns the OLDEST `first_mover_at`. The legitimate first mover (with `first_mover_at = 2026-05-10`) loses to the backdated late-comer.
**Impact:** First-mover delta multiplier (CONTRACTS 7.2) is the entire anti-copy mechanism for v1. This vulnerability inverts it: copy attackers backdate themselves into the originator slot, and the originator becomes a "late copier" who suffers the 0.50 multiplier. Originator incentive collapses immediately on day one of launch.
**Recommended fix:** Remove the client-supplied `submitted_at` fallback. The signature MUST be over the server clock. Require the client to first GET a `/v1/server-time` endpoint (returning a server-signed timestamp) and sign over THAT value if clock skew is the concern. Ideally use a server-issued nonce instead of timestamp-as-nonce.
**CONTRACTS section violated:** Section 4.1 (signed payload includes `submitted_at`); Section 7.2 (first-mover delta).

---

### [CRIT-2] Eval tasks are deterministic from public inputs - miners pre-compute every future task
**Where:** `src/cathedral/eval/task_generator.py:28-67`
**What:** `generate_task` seeds a `random.Random` with `blake3(f"{card_id}|{epoch}|{round_index}").digest()[:8]` and then picks templates and sources from `card_definition.task_templates` / `source_pool`. Every input is public:
- `card_id` from `GET /v1/cards`
- `epoch` from clock arithmetic (`epoch_for(dt)`)
- `round_index` is a monotonic public counter (visible in eval_runs)
- `task_templates` + `source_pool` from `GET /v1/cards/{card_id}/eval-spec`
**How to reproduce:** `tests/v1/exploits/eval_task_predictable_pre_compute.py` enumerates the next ~10 tasks for a sample card_definition. They are byte-identical across runs.
**Impact:** CONTRACTS critical guardrail #1 - "Eval set MUST NOT be frozen. Source pool refreshes per round. Task templates produce novel queries" - is functionally violated. The set is enumerable in advance. A miner can pre-compute every output for the next year, cache it in-bundle, and respond instantly with whatever "freshness" the scorer wants. This is the SN62 trained-to-test failure mode in a different costume.
**Recommended fix:** Inject server-side randomness into the seed that the miner cannot predict (e.g., a per-round nonce committed at round start, revealed after eval submission). OR use the on-chain block hash at round start as additional entropy. OR rotate the source_pool per epoch from a private pool of pools (so the public spec is "any 5 of the 200 sources we may pick this week").
**CONTRACTS section violated:** "Critical guardrails" #1; "Locked design choices" row on eval set.

---

### [CRIT-3] Inline-card path on legacy `/v1/claim` has no hotkey signature; bearer is the only auth
**Where:** `src/cathedral/types.py:35-74`, `src/cathedral/validator/app.py:87-93`, `src/cathedral/validator/auth.py:1-18`
**What:** The legacy `PolarisAgentClaim` endpoint has no `signature` field. Bearer token is shared and per-validator, not per-miner. Even after the new `/v1/agents/submit` ships with sr25519 signatures, `/v1/claim` remains the path described in the old in-repo docs at review time; until it's removed or hardened, anyone with the bearer can attribute claims to any hotkey.
**How to reproduce:** `tests/v1/exploits/no_hotkey_signature.py`. Returns 202 with arbitrary `miner_hotkey="5Victim"`.
**Impact:** Two paths exist for claim submission:
1. The new v1 `/v1/agents/submit` (sr25519-signed)
2. The legacy `/v1/claim` (bearer only)
Until path #2 is deleted, an attacker bypasses all of v1's auth by using path #2.
**Recommended fix:** Either (a) delete `/v1/claim` and `cathedral.miner.submit` before launch, OR (b) require sr25519 signature on `/v1/claim` too. Document which is the intended path in v1.
**CONTRACTS section violated:** Section 4.1.

---

### [CRIT-4] `card_id` mapping via `work_unit` overrides Card.id; cards filed under wrong card_id
**Where:** `src/cathedral/validator/worker.py:113-127` (`_coerce_card`)
**What:** Worker forces `card_id = work_unit.removeprefix("card:")` onto the inline payload, overriding the body's `id` field. A miner submitting body for `us-ccpa` under `work_unit=card:eu-ai-act` gets the CCPA content stored as `eu-ai-act`.
**How to reproduce:** `tests/v1/exploits/work_unit_card_id_swap.py`. CCPA content stored under `card_id='eu-ai-act'`.
**Impact:** Public read endpoints return wrong-content-for-card_id. The new `/v1/cards/{card_id}` from `cathedral.publisher.reads` reads from the `agent_submissions` + `eval_runs` tables, which use a different storage path - but as long as the legacy `cards` table is also surfaced (it is, via `cathedral.validator.cards`), this is exploitable.
**Recommended fix:** Validate that `work_unit` matches a card_definitions row, that the inline payload's `id` (if present) matches `work_unit`, and that the payload's `jurisdiction` matches the registry entry.
**CONTRACTS section violated:** Section 6 step 4.

---

### [CRIT-5] Citation `status` and `class` are miner-controlled with no re-fetch
**Where:** `src/cathedral/cards/preflight.py:60-62`, `src/cathedral/types.py:189-196`
**What:** `preflight` accepts the miner's self-reported `status` integer and source `class_` enum. No URL is HEAD/GET-checked. A miner submits citations to `https://this-domain-does-not-exist.invalid/fake` with `class=official_journal` and `status=200` and earns full source_quality + coverage bonus.
**How to reproduce:** `tests/v1/exploits/citation_status_spoofing.py`. weighted=1.000 with bogus citations.
**Impact:** The "official source quality" gate is fictional. Combined with CRIT-2, a miner submits fully-fabricated cards with fake citation classes and gets max score deterministically.
**Recommended fix:**
- Cathedral-side HEAD/GET to verify status (cache aggressively).
- Domain allowlist per `SourceClass`.
- Verify `content_hash` against bytes Cathedral fetches.
**CONTRACTS section violated:** Section 8 scoring rubric.

---

### [CRIT-6] First-mover delta is computed by `metadata_fingerprint` (display_name + size_bucket) - trivially defeated
**Where:** `src/cathedral/publisher/similarity.py:38-53`, `submit.py:252-269`
**What:** First-mover status is anchored on `metadata_fingerprint = blake3(normalized_display_name | bundle_size_bucket_1k)`. To defeat this from the COPY side, the attacker:
- Picks a different display_name (Levenshtein distance > 0.85 from the original - easy with a single-char diff in a long name).
- Pads or trims the bundle by 1024 bytes to land in a different size bucket.
Result: `metadata_fingerprint` differs, similarity check passes, AND `first_mover_at = now()` is fresh - so the COPY becomes its own "first mover" for a different fingerprint, gets the 1.0 multiplier, and competes with the original on equal terms.
**How to reproduce:** Conceptually clear from the similarity.py code. The fingerprint is purely public-surface metadata, not bundle content. Bundle hash IS distinct (different padding), so `find_existing_bundle_hash` doesn't fire either.
**Impact:** First-mover delta + similarity check do nothing against a copy attacker who renames the bundle and adjusts size by 1 KiB. CONTRACTS guardrails #2 and #3 again, by mechanism rather than absence.
**Recommended fix:** Anchor first-mover by something the copier cannot trivially mutate. Options:
- `soul_md_preview` semantic hash (after bundle decryption during eval) - but that's eval-time, not submit-time.
- Hash of normalized soul.md content extracted server-side during validation.
- LLM-judge similarity on bundle contents at eval time.
For v1, at minimum: bucket by 100 KiB instead of 1 KiB, and use a hash of the FULL display name plus bio plus logo_url length together.
**CONTRACTS section violated:** Section 7.1 / 7.2 (similarity + first-mover).

---

## HIGH findings

### [HIGH-1] `/v1/claim` (legacy) silently swallows resubmissions via `INSERT ON CONFLICT DO NOTHING`
**Where:** `src/cathedral/validator/queue.py:23-55`
**What:** A second submit with same `(miner_hotkey, work_unit, polaris_agent_id)` returns 202 + the original id but the new payload is dropped. Combined with CRIT-3 (no auth), an attacker can preemptively lock any victim's slot to garbage.
**How to reproduce:** `tests/v1/exploits/duplicate_claim_swallows_update.py`.
**Impact:** Same as before; not catastrophic on its own but compounds CRIT-3.
**Recommended fix:** UPSERT and reset status to `pending` on conflict, OR return 409 explicitly so the caller knows to delete-and-resubmit.

---

### [HIGH-2] `winner-take-all` weighting on sparse scores
**Where:** `src/cathedral/chain/weights.py:8-18`
**What:** `normalize` divides by total. Single-positive-score input → that miner gets weight 1.0.
**How to reproduce:** `tests/v1/exploits/normalize_winner_take_all.py`. `[(1,0.92),(2,0),...]` → `[(1,1.0),...]`.
**Impact:** SN62 anti-pattern. CONTRACTS warns against it explicitly.
**Recommended fix:** Reserve 10% baseline distribution OR softmax with tunable temperature.

---

### [HIGH-3] `last_refreshed_at` in the future locks max freshness + maintenance forever
**Where:** `src/cathedral/cards/score.py:42-50, 83-92`
**What:** Negative age → ratio = 0 → freshness = maintenance = 1.0 forever. No bound check.
**How to reproduce:** `tests/v1/exploits/future_timestamp.py`. `last_refreshed_at = year 9999` → weighted = 1.0.
**Recommended fix:** Reject `last_refreshed_at > now() + N min` clock-skew tolerance.

---

### [HIGH-4] Bundle compression-bomb heuristic misses entries < 1 MiB
**Where:** `src/cathedral/storage/bundle_extractor.py:114-121`
**What:** `if info.compress_size > 0 and info.file_size / max(1, info.compress_size) > 200 and info.file_size > 1*1024*1024`. The third condition (size > 1 MiB) means ratios up to infinity are allowed for entries ≤ 1 MiB. A bundle of 95 entries each 1 MiB-1 (compressing to ~1 KiB each) packs ~95 MiB of inflation into 100 KiB on the wire.
**How to reproduce:** `tests/v1/exploits/zip_bomb_evades_per_file_check.py`. 108 KiB zip with 95 MiB total inflation accepted.
**Impact:** N concurrent submissions with this bundle = N × 95 MiB ephemeral disk consumption during eval extraction. The 100 MiB total cap blocks the absolute worst case but allows linear DoS.
**Recommended fix:** Drop the per-file size guard from the bomb check - apply ratio threshold to ALL entries. Track aggregate ratio across the whole bundle and reject above 50x overall.

---

### [HIGH-5] Logo upload trusts client Content-Type; stored XSS via PNG-typed HTML
**Where:** `src/cathedral/publisher/submit.py:204-229`, `cathedral/storage/hippius_client.py:166-186`
**What:** `logo.content_type` is checked against an allowlist but never sniffed against magic bytes. The bytes are uploaded to Hippius with `ACL=public-read` and `ContentType=` whatever the client claimed. If a Hippius gateway honors HTML rendering for `Content-Type: image/png` payloads (or has lax X-Content-Type-Options), this is stored XSS via the public logo URL.
**How to reproduce:** Conceptual, see `tests/v1/exploits/logo_xss_via_content_type.py`. Polyglot: PNG header bytes followed by `<script>...`.
**Impact:** XSS in cathedral.computer if the frontend ever embeds the URL via `<object>`, `<iframe>`, or directly opens it in a new tab. Phishing vector regardless.
**Recommended fix:** PIL re-encode logos server-side; never trust client Content-Type. Set `Content-Disposition: attachment` on logos via Hippius bucket policy, OR serve logos through Cathedral with strict `X-Content-Type-Options: nosniff`.

---

### [HIGH-6] Polaris record types use `extra="allow"` - signed extras pass verification
**Where:** `src/cathedral/types.py:82-137` (PolarisManifest, PolarisRunRecord, PolarisArtifactRecord, PolarisUsageRecord all use `model_config = ConfigDict(extra="allow")`)
**What:** Pydantic `extra="allow"` lets the signer attach arbitrary extra fields that flow through `model_dump` and into the verifier-trusted dict. Future Cathedral code reading `manifest.<extra>` would trust attacker-injected data.
**How to reproduce:** `tests/v1/exploits/canonical_json_default_str.py`. Smuggled `cathedral_admin: true` field passes verify_manifest.
**Recommended fix:** `extra="forbid"` everywhere.

---

### [HIGH-7] No rate limit on legacy `/v1/claim`; queue grows unbounded
**Where:** `src/cathedral/validator/app.py:87-93`
**What:** A single bearer-holder can submit at line speed; sqlite WAL grows linearly.
**How to reproduce:** `tests/v1/exploits/queue_dos.py`. 1000 claims, 50KB inline payloads each, 50 MB on disk in 1.2 sec.
**Recommended fix:** Per-hotkey rate limit (after CRIT-3 fix), max payload size, queue depth limit.

---

### [HIGH-8] `cathedral.eval` package has a circular import - orchestrator + publisher cannot both load
**Where:** `src/cathedral/eval/__init__.py:3` imports `cathedral.eval.orchestrator` which imports `cathedral.publisher` which imports `cathedral.publisher.app` which imports `cathedral.eval.orchestrator.run_eval_loop`.
**What:** Importing `cathedral.eval` from a fresh interpreter raises `ImportError: cannot import name 'run_eval_loop' from partially initialized module 'cathedral.eval.orchestrator'`. This means the publisher app cannot start in production, and any tests that import `cathedral.eval` directly fail.
**How to reproduce:** `python -c 'import cathedral.eval'`. Triggers the cycle.
**Impact:** v1 launch service is unstartable until this is broken (typically via `if TYPE_CHECKING` for the back-edge import or by extracting the shared interface to a third module).
**Recommended fix:** Move `run_eval_loop` registration into a small adapter module (`cathedral.eval.runtime`) that publisher.app imports; orchestrator imports neither publisher.app nor publisher.__init__.

---

## MEDIUM findings

### [MED-1] `merkle_leaf` uses `str(weighted_score)` - float formatting drift breaks audit
**Where:** `src/cathedral/publisher/merkle.py:63-71`
**What:** Per CONTRACTS 4.5, leaf = `blake3(":".join([id, output_card_hash, str(weighted_score), cathedral_signature]))`. `str(0.85)` is `'0.85'` in CPython 3.11+, but `str(1/3)` is `'0.3333333333333333'`. If `weighted_score` is computed with slightly different float arithmetic across machines (e.g., due to numpy involvement someday), `str()` differs and the leaf hash diverges. Validators can no longer verify the published root.
**Impact:** Latent. Consensus break under future scoring refactor.
**Recommended fix:** Format weighted_score with explicit precision: `f"{weighted_score:.10f}"`. Document the format in CONTRACTS.

---

### [MED-2] `merkle_root([])` returns `blake3(b"").hexdigest()` for every empty epoch
**Where:** `src/cathedral/publisher/merkle.py:74-86`
**What:** All empty epochs commit the same on-chain root. An attacker who replays an old `system.remarkWithEvent` extrinsic from an empty epoch can claim it represents a different empty epoch (pre-image is just blake3 of empty bytes).
**Impact:** Low. The on-chain commit format `cath:v1: || epoch_be32 || root` includes the epoch number, so the replay would commit a wrong-epoch root and a validator should reject the mismatched anchor. But the on-chain index is by extrinsic block, and ANY empty-epoch commit is byte-identical for the root portion.
**Recommended fix:** For empty epochs, use a domain-separator: `blake3(b"empty:" + epoch.to_bytes(4, 'big')).hexdigest()`.

---

### [MED-3] AES-GCM nonce stored both in blob prefix and key_id field; if either is corrupted, decrypt fails
**Where:** `src/cathedral/storage/crypto.py:115-140`
**What:** `decrypt_bundle` requires `nonce_in_blob == nonce_in_key`. This is double-storage, not a vulnerability per se. But if the database row's `encryption_key_id` is rewritten (e.g., manual ops fix-up) while Hippius blob is untouched, decryption permanently fails for that submission. There's no recovery path; the bundle is lost.
**Impact:** Operational fragility. An ops mistake bricks a miner's submission.
**Recommended fix:** Store the nonce in only one place. Either:
- Drop nonce from `encryption_key_id` and read it solely from blob prefix.
- Drop nonce from blob prefix and read it solely from key_id.

---

### [MED-4] `decrypt_bundle` requires loading master KEK from env on every call
**Where:** `src/cathedral/storage/crypto.py:115-140` calls `_load_master_kek()` per decrypt
**What:** If `CATHEDRAL_KEK_HEX` is unset at any point during runtime (e.g., env reload), decryption fails for ALL submissions, not just new ones. Cached data key recovery is impossible.
**Impact:** Operational. Combined with single-process deployment model, this is a single point of failure for the whole bundle store.
**Recommended fix:** Load KEK once at startup; fail fast at that point. Keep the loaded KEK in `HippiusContext` or similar.

---

### [MED-5] `repository.list_submissions_for_card` interpolates `sort` into SQL string
**Where:** `src/cathedral/publisher/repository.py:243-249`
**What:** `f"...ORDER BY {order} LIMIT ? OFFSET ?"` - `order` is whitelisted via if/elif, so this is currently safe. Comment says `# noqa: S608 - sort whitelisted above`. But the pattern is fragile; one developer adding a new sort option without an `elif` branch creates SQL injection. Worth refactoring to a lookup dict.
**Impact:** Latent SQL injection if sort whitelist drifts.
**Recommended fix:** `_SORT_TO_ORDER = {"score": "...", "recent": "...", "oldest": "..."}` then `order = _SORT_TO_ORDER[sort]` - KeyError on unknown is the right behavior.

---

### [MED-6] Hippius S3 client never validates returned bucket policies
**Where:** `src/cathedral/storage/hippius_client.py`
**What:** `put_bundle` writes encrypted bundle with no ACL parameter, defaulting to bucket default ACL. If the bucket is misconfigured to public-read (operator error, or Hippius default), encrypted bundles are world-readable. With offline brute force on the wrapped key (16-byte AES-key-wrap is hardened, but if the master KEK is also leaked, all bundles fall).
**Impact:** Defense-in-depth missing. If the bucket is misconfigured, the AES-256-GCM encryption is the only barrier.
**Recommended fix:** Set `ACL='private'` explicitly on `put_bundle`. Add a startup health-check that does a `get_bucket_acl` and asserts not public-read.

---

### [MED-7] `safe_extract_zip` does not enforce per-call cap on extraction wall time
**Where:** `src/cathedral/storage/bundle_extractor.py:134-189`
**What:** The extractor reads in 64 KiB chunks and tracks bytes-written, but has no time bound. A pathological zip with high-CPU compression formats (LZMA via deflate64?) could spin while extracting.
**Impact:** Latent CPU DoS during extraction.
**Recommended fix:** Wrap extraction in `asyncio.wait_for(..., timeout=N seconds)` from the caller side. The bundle extractor is sync today; add an async wrapper.

---

### [MED-8] `confidence > 1.0` and `confidence < 0` rejected by Pydantic - but `_usefulness` still bonuses 0.2 for `confidence > 0.5`
**Where:** `src/cathedral/cards/score.py:64-72`
**What:** `_usefulness` adds 0.2 if `confidence > 0.5`. Pydantic blocks out-of-range, so this is currently safe. But if a future schema migration drops the `Field(ge=0, le=1)` constraint, a miner submitting `confidence=1e10` would get a binary 0.2 bonus AND the high score becomes weight-amplified. Worth pinning the dimension scorer to a clamped value too.

---

## LOW findings

### [LOW-1] `/health` is unauthenticated and leaks claim counts (legacy validator)
**Where:** `src/cathedral/validator/app.py:83-85`
**What:** Information disclosure for monitoring; minor.

### [LOW-2] Bearer compared with `==` not `secrets.compare_digest`
**Where:** `src/cathedral/validator/auth.py:11-16`
**What:** Timing-attack-vulnerable string equality. Compounds CRIT-3.

### [LOW-3] `MissingRecordError` carries internal Polaris path string
**Where:** `src/cathedral/evidence/fetch.py:78-86`
**What:** Path leaked if any handler ever surfaces the exception message. Currently contained.

### [LOW-4] `httpx.AsyncClient` constructed once with single timeout
**Where:** `src/cathedral/evidence/fetch.py:43-48`
**What:** Slow-Polaris DoS partially mitigated by `max_concurrent_verifications`.

### [LOW-5] Single-key Polaris pubkey config; rotation breaks all in-flight claims
**Where:** `src/cathedral/validator/app.py:128`
**What:** Operational, not security.

### [LOW-6] `card_definition.deadline_minutes` read from card_definition dict but not validated
**Where:** `src/cathedral/eval/task_generator.py:59`
**What:** A cathedral team member uploading a malformed card_definition with `deadline_minutes = -1` causes the eval orchestrator to deadline-instantly. Not a miner-controllable field, so low priority.

---

## Things I tested that ARE secure
- Polaris record signature verify (`verify_manifest`/`verify_run`/`verify_artifact`/`verify_usage`) correctly rejects tampered records and wrong-pubkey verification (`tests/test_polaris_contract.py`).
- BLAKE3 artifact-bytes hash check.
- AES-256-GCM bundle encryption with per-bundle data key + KEK wrapping is correctly implemented (`crypto.py`); the cipher choice and AEAD usage are textbook.
- Bundle path-traversal check rejects `../`, `..\`, leading `/`, and Windows drive-letter paths.
- Bundle symlink check (mode 0o120000) blocks the simplest symlink attack.
- Bundle extraction `target.relative_to(dest_root_real)` defense-in-depth catches resolved escapes.
- `encode_anchor_payload` validates epoch range and merkle_root length (32 bytes hex).
- sr25519 hotkey signature verification via `substrateinterface.Keypair(ss58_address=...).verify(...)` is the standard Bittensor pattern.
- Pydantic `extra="forbid"` is correctly used on all v1_types models (AgentSubmission, EvalTask, EvalRun, etc.) - only the legacy Polaris record types use `extra="allow"`.
- Display-name fuzzy matching (Levenshtein ratio ≥ 0.85) catches naive copies; only adversarial copies that fly under the threshold + change size bucket evade.

---

## Things I couldn't test (gaps for the next pass)
- The Polaris HTTP runner against a real Polaris instance (`HttpPolarisRunner.run`). The cathedral-eval endpoint contract on the Polaris side is not in this repo.
- Real Hippius S3 with credentials - only the boto3 client is covered. Bucket-policy verification, IAM-policy traversal, signed-URL leakage all untested live.
- Real Bittensor `set_weights` / `system.remarkWithEvent` against testnet. The chain client paths are mocked in CI.
- Concurrent 100-miner load test against the publisher. The submit path is sync within a single request but the eval orchestrator is async; deadlock potential on the shared aiosqlite connection is plausible but unverified.
- LLM prompt injection in soul.md preview when surfaced to a future eval-judging LLM. Out of scope until the LLM-judge ships.
- Frontend (cathedral.computer) signing flow - backend doesn't verify cleanly enough yet (CRIT-1) for frontend tests to mean anything.

---

## Out of scope but worth filing
- Polaris cathedral-eval endpoint shape (`POST /polaris/agents/cathedral-eval`) is described in `polaris_runner.py` but I have no way to verify it matches what the polariscomputer repo actually ships. Cross-repo contract test needed (similar to `test_polaris_contract.py` for the Polaris-record signing format).
- The `cathedral-eval-spec` content repo is referenced as the source of `card_definitions` rows. Its provisioning workflow isn't in this repo.
- Frontend XSS surface beyond the logo upload (HTML injection via display_name, bio, card content) - not reviewed.
- Card content quality (factual accuracy, hallucination, bias) - out of scope for a security review.

---

## Coordination notes

### Findings the IMPLEMENTER should action immediately (before merge)
1. **CRIT-1** (backdated submitted_at) - one-line removal of the fallback path.
2. **CRIT-2** (deterministic eval tasks) - design discussion required; CONTRACTS guardrail.
3. **CRIT-3** (legacy `/v1/claim` no auth) - decide whether to delete the endpoint or harden it. Decide BEFORE launch.
4. **CRIT-6** (first-mover defeated by metadata change) - need a fingerprint that bites copies.
5. **HIGH-8** (circular import) - service won't start.

### Findings to flag for Fred review (architectural / scope decisions)
- The legacy validator + new publisher are TWO services with overlapping but inconsistent surface. CRIT-3 + CRIT-4 both stem from the legacy path being soft-auth while the new path is hard-auth. Clear deprecation path needed.
- CRIT-2 is the deepest issue: full-determinism eval tasks negate the "live" promise. Could be a v1.1 fix only if launch communications are honest about the limitation.
- HIGH-2 (winner-take-all) and CRIT-6 (first-mover anchor weakness) together mean the v1 economic incentive is unprotected. Worth a focused design review.

### Findings that contradict the CONTRACTS doc (warden problem)
- CONTRACTS Section 4.1 mandates `submitted_at` be in the signed payload to prevent replay; the implementer's `submit.py` allows the client to overwrite it via fallback. This silently weakens the contract.
- CONTRACTS Section 7.1 implies metadata fingerprint is a v1 anti-copy mechanism; in practice the fingerprint is too narrow (display_name + 1 KiB size bucket).
- CONTRACTS Section 4.5 specifies `str(weighted_score)` for leaf hashing; this is fragile (MED-1) but it IS what the doc says. If we want safety, the doc needs to lock the float format too.
- CONTRACTS implies eval tasks are unpredictable (live regulatory tasks). Implementation makes them fully deterministic from public state.

---

## Estimated implementer time to address findings

| Finding | Time |
|---------|------|
| CRIT-1 (backdated submitted_at) | 1 hour (delete fallback, update miner client to use server-issued timestamp) |
| CRIT-2 (deterministic tasks) | 1-2 days (server nonce or chain-block entropy, plus test scaffolding) |
| CRIT-3 (legacy /v1/claim) | 1 hour (delete) OR 1 day (sr25519 retrofit) |
| CRIT-4 (work_unit overrides Card.id) | 1 hour |
| CRIT-5 (citation re-fetch) | 1 day (HEAD checks, in-memory cache, allowlist config) |
| CRIT-6 (fingerprint anchor) | 1 day (extract soul.md hash server-side after decrypt; or content-based hash) |
| HIGH-1 (UPSERT) | 1 hour |
| HIGH-2 (winner-take-all) | 0.5 day (softmax floor) |
| HIGH-3 (future timestamp) | 30 min |
| HIGH-4 (zip bomb < 1 MiB) | 30 min (drop the size guard from ratio check) |
| HIGH-5 (logo XSS) | 0.5 day (PIL re-encode + Hippius bucket policy) |
| HIGH-6 (extra="forbid") | 30 min + test updates |
| HIGH-7 (rate limit + size cap) | 0.5 day |
| HIGH-8 (circular import) | 1 hour (extract shared interface) |
| MED-1..MED-8 | 1-1.5 days combined |
| LOW-1..LOW-6 | 0.5 day combined |

**Estimated total: 8-10 engineer-days for full hardening of the v1 launch surface.**
**Of that, about 1 day is launch-blocking (CRIT-1, CRIT-3 deletion path, HIGH-8) before any external user can touch the system without obvious exploit.**
