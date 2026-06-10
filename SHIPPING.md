# Cathedral v4 — The Shipping Manifest (2026-06-10)

*What's going out the door, why every line of it exists, and where the bodies
are buried. Companion to `STRATEGY.md` (the why) and `deploy/RUNBOOK.md` (the how).*

---

## The headline

A 46,500-line production monolith is replaced by a **~5k-line thin publisher**
that is byte-identical on the only surface that matters; a reward system that
paid an idle miner the same as one doing a thousand solves a day is fixed —
effective immediately, with no dependence on validator adoption; and the
strategy governing all of it is locked into the repo so it cannot be
re-litigated by a tired session. Around the launch: 18 GB of legacy CNFs
archived to GitHub and pruned off a 97%-full production volume, 41 dead
branches deleted, and the whole tree pushed as branch `v4` on
`cathedralai/cathedral`.

---

## Part I — The engine swap

**The invariant:** live SN39 validators consume exactly one thing — Ed25519-
signed eval rows from `api.cathedral.computer/v1/leaderboard/recent`, tuple
cursor `(ran_at,id)` strict-`>`, verified against the pinned production pubkey
(`10890a66…f25e26`). They do not care what produces the rows. Freeze that
surface and the backend swaps invisibly.

Frozen and proven, not assumed:

- `scaffold/wire.py` — byte-faithful port of canonical signing
  (`SIGNED_KEYS_V5` 14 keys; V6 += `challenge_value`/`solve_rank`/`solved`/
  `operator`; canonical JSON sort_keys + compact + `default=str`; Ed25519).
- `wire_compat.py` **8/8** — 50 real live rows (17 v5 / 33 v6) verify under
  the live production key using OUR canonicalization, incl. tamper checks.
- Every solve emits a v6 row + `-v5compat` mirror (`rows.py`) — the live
  convention, pixel-perfect to `fixtures/live-20260609/`.
- `seed_live.py` — seeds from the live surface, rows stored **verbatim with
  production signatures (never re-signed)**; idempotent, durable watermark →
  validator cursor continuity across the swap.
- `soak.py` — 4,000/4,000 live rows verified; 7-day per-hotkey divergence
  **0.0000**; miner smoke green.
- Miner surface unchanged: sr25519 6-field canonical claims (±300s skew,
  replay dedup), HMAC-tokenized constant-time CNF fetch, solve-on-submit
  witness verification, per-(hotkey,challenge) rate limit. Real
  `bittensor_wallet` backend — deploy gate refuses `stub-fail-closed`.

Dormant until post-cutover: **Lane S** (solver arena — source-hash-dedup
registry, certificate-gated batch runner, network-isolated sandbox salvaged
from the monolith's orphaned oracle jail, champion machine with strict-margin
dethrone; proven live when kissat 4.0.4 dethroned the stub through the full
certified loop), **Lane I** intake (quarantine 3, min batch score 0.5), and a
refill loop mirroring live's open-window retirement at exact live shape
(n=6000/m=25560, minted ~2s).

---

## Part II — The scoring reform: solve more, earn more

### The crime scene (measured on the production DB, 2026-06-10)

- Every solve scores a flat 1.0; validators average per-hotkey → 24 solves
  and 1,155 solves pay identically.
- The 7-day validator window lets rows outlive effort: **398 hotkeys solved
  in the last 48h but 510 sat in the window — 112 miners (22%) idle and still
  earning.** UID 63 was just the visible case.
- Pre-launch regression caught and killed: the thin publisher's submit
  **locked challenges on first valid solve** (winner-take-all) while live has
  been open-window since 2026-06-04 — would have 409'd 365 of 366 daily
  solvers at cutover.

### The fix (`scaffold/publisher/scoring.py` + rewired submit)

- **Open-window submit, live-faithful, default.** One solve per distinct
  hotkey per challenge, claimed atomically (the `INSERT OR IGNORE` into
  `lane_challenge_solves` IS the dedup), true first-seen `solve_rank` signed
  into the v6 row. Re-solve → 409 `already_solved`. Legacy lock-wins behind
  `CATHEDRAL_SUBMIT_MODE`.
- **Coverage policy, flag-gated** (`CATHEDRAL_SCORING_POLICY=coverage`):

  ```
  weighted_score = clamp( distinct challenges solved in trailing 24h
                          ─────────────────────────────────────────── , 0.1 … 1.0 )
                          challenges minted in the same window
  ```

  Solve everything offered → 1.0; half → 0.5; stop → decay. Window/floor
  env-tunable.

### Why it holds

1. **Immediate, validator-free effect.** Path-A validators average whatever
   row values we sign; coverage encodes work-share into those values, so
   their own aggregation delivers it. The same value rides v6
   `challenge_value` for Path-B.
2. **Arms race structurally impossible.** Each challenge is solvable once per
   hotkey → max score = board supply. The denominator is the cap.
3. **Matches measured behavior.** p50 time-to-first-solve = 40 min on
   seconds-easy CNFs: miners are lazy pollers optimizing presence, not speed.
   Coverage rewards exactly that; zero behavior change demanded.
4. **Sybil exposure unchanged** (k hotkeys → k×, same as flat 1.0).
5. **Correctness still pays** (floor 0.1 — a valid solve is never zero).

### The validator-update model (the honest part)

We CAN ship validator code whenever warranted — the constraint is **adoption
lag, not impossibility**. A release takes effect per-validator as operators
pull it; we can request updates for high-value reasons (major release,
jackpot) but never depend on timing. So the 7-day window CAN be killed
(→ instant/spot payment) via a release; until adoption completes, a stopped
miner keeps a frozen tail of ≤7 days per not-yet-updated validator. Policy
must stay correct under MIXED validator versions — coverage is (new rows
carry the policy; old validators just average them; nothing breaks).

### Remote weights & burn (verified against deployed validator code)

- **v4 does NOT reintroduce remote weight setting** — no
  `/v1/validator/weights/next` in the scaffold; validators aggregate locally.
- If/when burn is set remotely: validator schema already supports it —
  `BurnSnapshot.forced_burn_percentage: float, ge=0, le=100`,
  `burn_uid: int|None`, `extra="forbid"` (`policy/signing.py`); remote loop
  applies the vector's burn (`remote_weight_loop.py:353-360`). Flags that CAN
  fire: out-of-range/non-numeric burn, unexpected fields, wrong pinned
  `key_id`, wrong network/netuid, expired vector, non-increasing
  `policy_version`. Float in [0,100] + monotonic version → none fire. Reach:
  only validators with remote mode ON (opt-in, default OFF) — same adoption-
  lag model; burn changes ride the bundled release.

### Proof (gates run on the production container)

| Gate | Result |
|---|---|
| `rc_verify.py` | 35/35 |
| `wire_compat.py` | 8/8 |
| `publisher_verify.py` | **40/40** (8 new: open-window ranks, re-solve 409, coverage math) |

---

## Part III — The evidence base (live-DB excavation)

- **Real, exploding community:** 737 hotkeys ever → 523 activated (71%) →
  299 won. DAU 54 → **366** in 11 days; solves 168 → **21,948/day** (130×) —
  through outages, flat scoring, no docs. Top-10 = 13.7% share; median 51
  solves. Long tail, not whales.
- **Honest:** 1.1% copy signature; 0.4% rejects, dominated by DIMACS format
  friction (`malformed_answer`). The 14.2 GB of stored solutions is 64×
  redundancy on 2,669 actually-solved challenges, not fraud.
- **Nobody races:** p50 first-solve 40 min → speed mechanics are fiction.
- **Supply was theater:** 92% of 37,853 minted challenges expired untouched.

This is why the ship contains coverage scoring and NOT difficulty tiers,
racing, or a supply buildout. The users voted; we counted.

---

## Part IV — Strategy locked (`STRATEGY.md`)

Anti-pivot rule: never let the asset push for a use; let demand pull. Three
tracks: **attestor = product** (partner-driven: A6 async, offline collateral,
spot pool, receipt permalinks; demand-pump = wash-trading, rejected in
writing) · **Lane A = community** (this launch + reference miner kit) ·
**Lane F = what the burn buys, gated on the Bright/Ganesh email** (the fleet's
measured profile is exactly C&C's worker profile; frontiers move by method
not compute; no cube infra before a credible partner says yes). Plus the
retired list with falsifications so nothing regenerates.

## Part V — Attestation spec (`ATTESTATION.md`)

Attest only the unfalsifiable (producer / wall-clock / timeout); gate the
title, not the submission. `report_data[0:32]=sha256(nonce‖pubkey)`,
`[32:64]=sha256(solver_digest‖sha256(receipt))` — digest computed in-box,
offline Intel-collateral verify, in-TEE JSON receipt binds challenge-id +
wall-time tamper-evidently. Azure/MAA surfaced as an explicit decision
(recommended out of v1). Tripwires: publisher read-path is a PREREQUISITE for
verify-at-volume; no "Polaris-blind"/KBS claims without HSM custody.

---

## Part VI — Ops carnage

- `/data` was at **97%** (2.2 GB free) on the single-write-lock SQLite store:
  36,472 retired-challenge CNFs (18 GB) never cleaned up + 25 GB publisher.db
  (14.2 GB = the 64×-duplicated solutions) + stale backups + bloated WAL.
- **Archive-then-prune:** all CNFs tar'd on the container's ephemeral disk,
  5 parts + SHA256SUMS (one truncated transfer caught by checksum, re-pulled),
  published to `wallscaler/cathedral-cnf-archive` release `cnf-flood-2026-06`
  (7.6 GB). Prune kept the live active set exactly (51 ids from the live API
  at T-0), deleted 36,418 orphans, removed stale backups, checkpointed WAL:
  **97% → 57%, 20 GB free**, health 200×3 + full board after. Solutions
  remain in the DB untouched (the 2,669 dedup is the flagged future DB-shrink).
- **Repo hygiene:** `cathedralai/cathedral` 40 branches/3 PRs/10 issues →
  `main` + `v4`, 0 PRs, 4 roadmap issues (#236/237/241/251). All deleted SHAs
  recorded for recovery. Scaffold purged of the falsified bug-hunt arc; README
  + RELEASE_STATUS rewritten to describe the system being shipped.

## Part VII — The cutover (`deploy/RUNBOOK.md`)

Stage second Railway service → seed (verbatim rows, continuous watermark) →
soak green → 🔑 production signing key → 🔀 move `api.cathedral.computer`.
Monolith stays warm; rollback = move the domain back, minutes, no data
migration. Abort criteria non-negotiable: signature rejection / cursor stall /
divergence ≥0.10 / miner-path failure → flip back, no debate. Then the second
act, deliberately separate: `CATHEDRAL_SCORING_POLICY=coverage` — one env var
on OUR service — and fair pay goes live for 366 miners.

## Not in this ship (by design)

Burn step-down (the bundled validator release) · window-kill → instant pay
(same release; adoption-lag until then) · Lane S/I activation · the
throughput keystone (file-backed import + read path — next build;
prerequisite for cubes and attestation-at-volume) · reference miner kit ·
the Bright/Ganesh email (Fred's signature, not the agent's).

---

**One sentence:** a 10×-smaller engine on provably identical wires, fair pay
that needs nobody's permission, a strategy that can't be un-decided by a bad
mood — and the production volume no longer on fire.
