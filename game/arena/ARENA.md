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
python -m game.arena --season 3            # snapshot: 3-round season → out/arena.html + reports + screenshot
cathedral-arena --season 3                 # same after editable install
python -m game.arena --submitted           # real external agent PROCESSES sign+submit; arena verifies
python -m game.arena.audit 1               # independently audit scoring invariants
cathedral-arena-audit 1                    # same after editable install
python -m game.arena.bundle out/proof_bundle.json   # independently verify a winner's proof bundle
cathedral-arena-verify out/proof_bundle.json        # same after editable install
python -m pytest game/tests game/arena/tests -q     # full suite
```
UI: `out/arena.html` (screenshot `out/arena.png`). CATHEDRAL_ARENA_STITCH=1 routes
the stitch-runner agent's solve to a real kissat on Stitch.

Served game routes:

- `/`: auto-running arena render.
- `/game`: playable scanner game; sealing a proof calls `/api/scanner/submit`.
- `/dashboard.html`: legacy redirect to `/game` for old local links.

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
  family, witness/decode map, trace, optional human report.
- `cathedral.scanner.verdict.v1`: boolean gates, deterministic replay outcome,
  artifact hash, score.

Scoring is intentionally strict:

- prose reports are metadata only
- vulnerability category/family is a boolean alignment gate only
- score exists only when the witness reproduces against the pinned replay target

This is the clean bridge from "scanner/hunter app" to Cathedral-native proof:
miners can submit findings, but validators pay only replayable witnesses.

Local API:

- `GET /api/scanner/catalog?limit=2`
- `GET /api/scanner/task?index=0`
- `GET /api/scanner/example?index=0`
- `GET /api/scanner/agent/solve?index=0&miner_hotkey=...`
- `POST /api/scanner/replay`
- `POST /api/scanner/submit`
- `GET /api/scanner/leaderboard`
- `GET /api/scanner/submissions?limit=50`
- `GET /api/scanner/state?miner_hotkey=...`

Run with `python -m game.arena.serve 8800` and open `/game`. The server is
local/stdlib only and does not touch chain, Polaris, Railway, or production.
`/api/scanner/agent/solve` only produces a local demo proof artifact; score is
created only by posting that artifact to `/api/scanner/submit`. It accepts
`mode=valid|bad_witness|wrong_family|report_only` for gameplay and anti-cheat
tests. `/api/scanner/replay` is a dry-run deterministic verifier gate and does
not write the ledger.

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
| `engine.py` | the round: assign → operate → 14 gates → reward → assemble |
| `reward.py` (in `game/`) | Const compose, Sybil collapse, Ed25519 vector |
| `economy.py` / `season.py` | emissions/ranks; persistent cross-round seasons |
| `solverbench.py` | PAR-2 solver benchmark (real scaffold solver_arena) |
| `anchor.py` | Merkle round commitment + inclusion proofs |
| `bundle.py` | portable proof bundle + standalone verifier |
| `audit.py` | independent scoring invariant auditor |
| `ui.py` / `serve.py` | visual render; live HTTP server |

## Remaining Depth

Open depth items: (a) a real Pi/Hermes tool-use loop as the agent instead of
scripted local behaviors; (b) in-band live attestation in `run_submitted`
binding the receipt head into a fresh TDX quote; (c) a gated on-chain Merkle-root
commitment; (d) production mapping from raw external DIMACS assignments back to
input bits for off-box miner solves.
