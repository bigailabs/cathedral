# Cathedral Arena

A live, visual miner-agent **verification arena** grounded in the real
audit-hunter subnet corpus. Miners operate **agents** that get a subnet target,
solve an encoded invariant, submit a signed proof, and earn emissions only when
every gate passes. **Cathedral rewards proof, not claims** — and a round's output
is a portable proof object anyone can verify with no access to the engine.

Built on the local game (`game/`, see `GAME_SPEC.md`). Repo-local runs use the
adjacent `audit-hunter` corpus when available; installed wheels fall back to a
small bundled target/proof corpus so the arena still runs without private local
artifacts.

## Run it

```bash
python -m game.arena.serve                 # LIVE server → http://127.0.0.1:8800 (ticks a fresh round on refresh)
cathedral-arena-serve 8800                 # same after editable install
python -m game.arena --season 3            # snapshot: 3-round season → out/arena.html + reports
python -m game.arena --shot                # report screenshot + playable /game screenshot manifests
cathedral-arena --season 3                 # same after editable install
python -m game.arena --submitted           # real external agent PROCESSES sign+submit; arena verifies
python -m game.arena.playthrough           # machine-check the scoreful /game loop
python -m game.arena.audit_scanner_smoke   # machine-check the signed /v1/audit-scanner bridge
python -m game.arena.audit 1               # independently audit scoring invariants
cathedral-arena-audit 1                    # same after editable install
python -m game.arena.bundle out/proof_bundle.json   # independently verify a winner's proof bundle
cathedral-arena-verify out/proof_bundle.json        # same after editable install
python -m game.arena.verify out                     # independently verify the full round artifact set
cathedral-arena-round-verify out                    # same after editable install
python -m game.arena.selfcheck out                  # one-shot operator health: "ARENA REAL & HEALTHY"
python -m game.arena.proofboard ; python -m game.arena.frontpage  # render the Proof Board / simple hub
cathedral-arena-playthrough                # same after editable install
cathedral-audit-scanner-smoke              # same after editable install
python -m pytest game/tests game/arena/tests -q     # full suite
```
UI: `out/arena.html` (screenshot `out/arena.png`). The playthrough artifact is
`out/scanner_playthrough.json`. CATHEDRAL_ARENA_STITCH=1 routes
the stitch-runner agent's solve to a real kissat on Stitch.

Served game routes:

- `/`: redirects to `/game` so the first screen is the playable loop.
- `/home` or `/start`: plain-language hub linking to the game, guide, proof board, and arena.
- `/game`: playable scanner game; sealing a proof calls
  `/api/scanner/submit-attested`.
  It starts from `POST /api/scanner/request`, then routes that intake into
  replay-backed subnet targets.
- `/proofs`: human-readable Proof Board for `GET /api/scanner/differential`.
- `/arena`: auto-running arena report render.
- `/dashboard.html`: legacy redirect to `/game` for old local links.
- `/howto`: short game instructions.
- `/api/selfcheck` or `/healthz`: JSON operator health check; returns 200 when
  the required replay/proof gates are healthy and 503 when not.

Playable `/game` controls:

- `1 Probe`: fetch and scope the assigned subnet target.
- `2 Encode`: align the invariant family gate.
- `3 Solve`: create a replayable witness artifact.
- `4 Replay`: dry-run the deterministic verifier with no ledger write.
- `5 Attest`: bind a local simulated TEE receipt to the replayed proof.
- `6 Seal`: submit the attested artifact and write the local ledger.
- `7 Report`: submit a report-only claim; this is intentionally rejected because
  there is no witness/decode map.
- `8 Forge`: submit a corrupt witness; replay catches it.
- `9 Cooldown`: trade time for lower verifier heat.

The right-side verifier gate panel shows the current replay verdict as game
state: `PASS`, `FAIL`, or `WAIT` for the boolean gates that decide whether a
claim can seal.

The center phase badge shows the next required player action: `PROBE -> ENCODE
-> SOLVE -> REPLAY -> ATTEST -> SEAL -> SEALED`.

On reload, the game restores the local scanner ledger and resumes on the first
uncleared target instead of reopening an already sealed one.

After a successful seal, the game automatically advances to the next uncleared
subnet target; the end modal appears only when every local target is sealed.
`Run it back` clears the local browser player id before reloading, so a replayed
season starts with a fresh local player while the old ledger remains auditable.

## Scanner / Hunter Contract

`scanner.py` is the product-facing task contract inspired by Bitsec, but with
Cathedral's proof standard:

```
Bitsec shape:    code -> vulnerability report -> similarity score
Cathedral shape: target -> witness + trace -> deterministic replay score
```

Schemas:

- `cathedral.scanner.task.v1`: target, objective, pinned replay target,
  expected invariant family, required witness fields, nonce, bounty weight.
- `cathedral.scanner.submission.v1`: miner hotkey, nonce, committed proof
  family, witness/decode map, trace, optional structured claim, optional human
  report.
- `cathedral.scanner.claim.v1`: title, category, severity, location, impact,
  exploit summary, and fix summary. This is useful for humans and training
  data, but it is metadata only.
- `cathedral.scanner.verdict.v1`: boolean gates, deterministic replay outcome,
  artifact hash, score.
- `cathedral.scanner.benchmark.v1`: the live metric artifact. The metric is
  `replay_kill_rate`, not report quality.
- `cathedral.arena.replay_differential.v1`: verifier-quality artifact proving
  each replay harness separates exploit witnesses from benign witnesses, or
  holds across a conserved stress set.

Scoring is intentionally strict:

- prose reports are metadata only
- structured vulnerability claims are metadata only
- vulnerability category/family is a boolean alignment gate only
- score exists only when the witness reproduces against the pinned replay target
- leaderboard rows expose `kills`, `kill_rate`, and `weighted_kill_rate`; report-only
  attempts show `kills=0`

This is the clean bridge from "scanner/hunter app" to Cathedral-native proof:
miners can submit findings, but validators pay only replayable witnesses.

What we take from Bitsec:

- a simple scanner surface: repo/objective in, miner findings out
- typed findings with category, severity, exploit summary, and fix summary
- synthetic and real targets as a benchmark feed
- an organic request path for future customer scans

What we do not inherit:

- report similarity scoring
- category overlap as a reward metric
- LLM-generated expected answers as final truth
- public docs/config/API drift

The Cathedral rule is stricter: claims are useful metadata, but only a pinned
target plus witness plus deterministic replay can create score.

Local API:

- `GET /api/scanner/catalog?limit=2`
- `GET /api/scanner/task?index=0`
- `GET /api/scanner/example?index=0`
- `GET /api/scanner/agent/solve?task_id=scan-...&miner_hotkey=...`
- `POST /api/scanner/request`
- `POST /api/scanner/replay`
- `POST /api/scanner/attest`
- `POST /api/scanner/submit-attested`
- `POST /api/scanner/submit`
- `GET /api/scanner/leaderboard`
- `GET /api/scanner/benchmark`
- `GET /api/scanner/differential`
- `GET /api/scanner/submissions?limit=50`
- `GET /api/scanner/state?miner_hotkey=...`
- `GET /api/selfcheck` or `GET /healthz`

Run with `python -m game.arena.serve 8800` and open `/game`. The server is
local/stdlib only and does not touch chain, Polaris, Railway, or production.
`/api/scanner/request` is the organic scan intake surface: send repo/scope/objective
metadata and it returns replay-backed tasks. It is explicitly unscored and writes
no ledger row until a miner later submits a replayable witness.
`/api/scanner/agent/solve` only produces a local demo proof artifact; score is
created in the playable game only by replaying, attesting, then posting that
artifact to `/api/scanner/submit-attested`. The lower-level `/api/scanner/submit`
endpoint remains available as a raw scanner primitive. It accepts
`mode=valid|bad_witness|wrong_family|report_only` for gameplay and anti-cheat
tests. `/api/scanner/replay` is a dry-run deterministic verifier gate and does
not write the ledger. `/api/scanner/attest` issues a local simulated TEE receipt
for a replayed proof; it is labeled non-production and is checked again by
`/api/scanner/submit-attested`.

The local `/game` route and backing APIs are tested together: a report-only claim
fails replay, the same target can recover with a valid witness, sealing updates
score and kill rate, and successful seals do not trigger heat rollback.

Production-style bridge:

- `GET /v1/audit-scanner/status` is always available.
- Set `CATHEDRAL_AUDIT_SCANNER_ENABLED=1` to enable the remaining routes.
- `GET /v1/audit-scanner/catalog?limit=2`
- `GET /v1/audit-scanner/example?index=0`
- `POST /v1/audit-scanner/replay`
- `POST /v1/audit-scanner/submit`
- `GET /v1/audit-scanner/leaderboard`
- `GET /v1/audit-scanner/benchmark`
- `GET /v1/audit-scanner/differential`
- `GET /v1/audit-scanner/submissions?limit=50`
- `GET /v1/audit-scanner/state?miner_hotkey=...`

Run `python -m game.arena.audit_scanner_smoke` to exercise this bridge
in-process with a real sr25519 hotkey signature. Run
`python -m game.arena.audit_scanner_smoke --url http://127.0.0.1:8000` to
probe a running publisher. The bridge is deliberately `payment_weights=false`
until it is promoted into the signed weight policy. The submissions endpoint is
hash-only ledger evidence: it exposes verdict rows, not raw witnesses or reports.

## The proof chain (every link is real, all tested)

```
encode invariant (z3 factory mint)          mint.py  ← audit-hunter/factory + models
   → real CDCL solver solves the CNF (Glucose, verified)        mint.solve_minted_cnf
   → the solution is the exploit input → REAL harness reproduces  replay.py (U64F64 + audit_lane)
   → agent signs a hash-chained run-receipt                       provenance.py / agent_cli.py
   → 14 boolean gates verify the submission                       engine._gates
   → reward = linear_metric × boolean_gate                        reward.py (Sybil-collapsed)
   → Ed25519-signed weight vector (emission)                      reward.sign_vector
   → Merkle-anchored round commitment                             anchor.py
   → portable proof bundle, verifiable with NO engine             bundle.py
   → full round artifact verification, with NO engine              verify.py
```

## Scoring (Const rule)

`reward_i = Σ_missions metric(m) × GATE(m)` where `metric = tier_weight × speed ×
bounty` and `GATE = AND of 14 booleans`. Emissions = `weight × pool ×
provenance_mult + target bounty` (drained on a breach); ranks Initiate→Cathedral
Breaker; persistent seasons.

## The 14 gates / anti-cheat (every named cheat → a UI-visible rejection)

| gate | cheat it stops | archetype |
|---|---|---|
| valid_identity | unregistered / impostor key | (delegation) |
| assigned_mission | off-mission | — |
| fresh_nonce | **stale replay** (TTL) | cricket |
| no_replay | **spam** / duplicate | locust |
| correct_owner | **wrong-owner** | jackdaw |
| complete_artifact | malformed | — |
| cnf_hash_matches | **invalid CNF** | weevil |
| decode_map_present | **missing decode map** | termite |
| witness_verifies | **copied witness** | magpie |
| replay_succeeds | **invalid replay harness** | hornet |
| compute_profile_honest | **fake compute profile** | wasp |
| attestation_valid | **fake attestation** | cuckoo |
| agent_signature_valid | **trace forgery** | mantis |
| provenance_chain_intact | tampered chain | mantis |
| (coldkey collapse) | **hotkey stacking** | swarm-a/b |

## Real vs mocked (operator console, honest)

- **Real in the default local path:** deterministic replay harnesses, U64F64
  money-math ports, minted CNF/decode-map checks when solver deps are available,
  hotkey-to-agent-key delegation, hash-chained provenance, Ed25519 emissions,
  Merkle anchor, portable proof bundle, and solver-bench checks.
- **Real when local/private artifacts are present:** the adjacent `audit-hunter`
  corpus, money-math CNF manifests, Stitch/kissat execution receipts, and TDX
  receipt verification fixtures/status files.
- **Mocked/labeled:** mocked-tee env and scripted agent behaviors. A real
  Pi/Hermes tool-use loop is the next realness step.
- **Safe:** local/sandbox/testnet only; no mainnet writes; live SN39 (UID200)
  never contacted. On-chain anchor write is DEFERRED.

## Modules

| file | role |
|---|---|
| `corpus.py` | load audit-hunter targets/CNFs, with bundled fallback targets |
| `mint.py` | z3 factory mint + real CDCL solve + unified-proof status |
| `replay.py` | real money-math + audit_lane + minted replay harnesses |
| `roster.py` / `agent_cli.py` | 15 agent archetypes; real external signing agent |
| `provenance.py` | agent identity, signed hash-chained run-receipts, verify-by-receipt |
| `attestation.py` | real DCAP verifier path + live TDX quote status |
| `stitch.py` | real remote kissat execution env (host-measured) |
| `scanner.py` | scanner task/submission/verdict contract + replay-kill leaderboard |
| `reports.py` | self-auditing score report + anti-cheat report deliverables |
| `screenshot.py` | best-effort Edge screenshot + machine-checkable manifest |
| `engine.py` | the round: assign → operate → 14 gates → reward → assemble |
| `reward.py` (in `game/`) | Const compose, Sybil collapse, Ed25519 vector |
| `economy.py` / `season.py` | emissions/ranks; persistent cross-round seasons |
| `solverbench.py` | PAR-2 solver benchmark (real scaffold solver_arena) |
| `anchor.py` | Merkle round commitment + inclusion proofs |
| `bundle.py` | portable proof bundle + standalone verifier |
| `verify.py` | offline verifier for the generated round artifact set |
| `audit.py` | independent scoring invariant auditor |
| `ui.py` / `serve.py` | visual render; live HTTP server |

## Off-box solves on Stitch — LANDED (real, live-captured)

kissat on Stitch (real remote hardware) solves a z3-minted CNF; the arena decodes the
raw DIMACS assignment back to the exploit input bits LOCALLY with NO z3 (the bit→var
decode map z3 emits at mint time → `mint.decode_assignment`), and re-checks it against
the pinned-invariant CNF (`cnf_satisfied`, solver/model-independent). Captured live in
BOTH directions and across rules/models. The sanitized evidence manifest is
`game/arena/offbox_handoff_receipts.json`; operator machines may also keep raw receipts
under ignored `game/arena/out/`. Rounds with raw receipts are independently re-checkable
by `python -m game.arena.verify --json` and surfaced in the operator console + real-audit
vault:

| receipt file | direction | rule | model | evidence |
|---|---|---|---|---|
| `offbox_stitch_receipt.json`   | CRACKED  | B2-fee-silent-zero  | amm  | kissat 2ms, 357 lits, decoded no-z3 |
| `offbox_i1_receipt.json`       | CRACKED  | I1-div-by-zero      | amm  | kissat 11ms, 4032 lits, decoded no-z3 |
| `offbox_hardened_receipt.json` | HARDENED | A4-fee-split-conservation | amm | kissat 39ms UNSAT + local CDCL UNSAT |

`mint.offbox_on_stitch(rule_id)` / `offbox_hardened_on_stitch(rule_id, model, width)` are
the entry points; `capture_offbox_receipt` / `capture_hardened_receipt` persist a receipt.
GOTCHA: z3's bit-blast CNF serialization is non-deterministic across PROCESSES, so a
receipt's `cnf_sha256` is NOT re-derivable by re-minting — do not add a hash-rebind gate
(the rigorous proof is `cnf_satisfied`, checked at capture). The root-staking invariants
are only UNSAT at width 8 (z3 'unknown' at 16) — pass `width=8` for `subtensor-root-reborn`.

## Remaining Depth

Open depth items: (a) a real Pi/Hermes tool-use loop as the agent instead of
scripted local behaviors; (b) in-band live attestation in `run_submitted`
binding the receipt head into a fresh TDX quote (the binding recipe is built;
only a FRESH per-round TDX quote is gated on the box + spend); (c) a gated
on-chain Merkle-root commitment; (d) a live root-model HARDENED off-box capture
(A4-tao-split @ width 8 — local CDCL confirms UNSAT; awaiting a stable Stitch
window for the multi-round-trip upload).
