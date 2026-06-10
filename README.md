# Cathedral v4 — the always-on, paid SAT Competition

> A standing world-record bounty machine for solver progress. Emission buys
> exactly one thing: **mathematically verified algorithmic progress beyond the
> current world frontier.** When there is none, it buys nothing.

Cathedral is a Bittensor subnet (SN39) rebuilt thin on this scaffold (~3k lines)
in place of the 46k-line monolith. Every payout path is closed by a **certificate**
— a SAT witness re-checked against the formula, or a DRAT/LRAT proof checked by
`drat-trim`. No judge, no opinion, no self-reported numbers. No progress → burn.

See [`V4-DESIGN.md`](V4-DESIGN.md) for the full design and the primary-source
evidence ([`V4-DESIGN.html`](V4-DESIGN.html) is the dummy-proof walkthrough).

## The four lanes

| Lane | What it pays for | How it's proven |
|---|---|---|
| **S — solver arena** (flagship) | An open-source solver that beats the reigning champion on a fresh hidden batch | PAR-2 + marginal-VBS, every result carries its certificate; dethrone only past a fixed margin → jackpot + burn steps down |
| **A — community challenges** | Certificate-checked solve-work, open to anyone with a box | Witness self-verifies; speed is **server-measured**, never miner-reported; champion solvers handed out as *designated solvers* |
| **I — breaker instances** | An instance that beats the champion | Pays only on **disagreement-proven hardness** (champion times out, another solver closes it with a valid cert); decaying payment + quarantine + min-batch-score anti-gaming |
| **F — frontier fleet** (research, built after S/A/I) | Trustless cube-and-conquer on open math problems (Kochen-Specker, Schur, Ramsey) | Per-cube LRAT, zero clause-sharing, check-then-hold proof custody |

## Trust model — the community is the referee (we run NO eval infrastructure)

No first-party eval fleet, no hosted inference. Three tiers:

1. **Unattested community work** — certificates make correctness free to verify;
   coverage is unfakeable (faking "solver X closed instance i" requires solving
   i anyway). Cohort racing via block-hash-assigned designated solvers gives
   relative speed signal.
2. **Attested work (opt-in multiplier)** — a miner inside a TDX runner (Polaris
   `/v1/attest`) binds (solver digest, instance, wall time). Attested
   submissions earn ×m. *The multiplier is the infrastructure budget.*
3. **Title matches & audits** — k-of-n attested quorum (median); the same
   population earns fees for fraud-proof spot-checks (rollup pattern for eval).

Validators stay thin: verify certificates + attestation signatures + compute
weights from public data (or consume the Path B signed vector during transition).

## How it reaches chain — near-zero validator updates

Live validators run **local weights**: they pull Ed25519-signed eval rows from
the publisher feed, aggregate 7-day, then apply the hardcoded 85% burn. So the
frozen surface is the **signed-row feed** — same URL, cursor, v5/v6 row schema,
signing key, and task-type vocabulary. v4's lane economics ride in the *row
values* we control. The one thing that needs a validator release is the burn
step-down, bundled into the first record-fall jackpot. See `V4-DESIGN.md` →
*Migration*, and [`COMPAT.md`](COMPAT.md) for the do-not-break freeze surface.

## Run it

```bash
python -m scaffold.live          # validator runner (real cryptominisat / drat-trim / z3 when present)
python -m scaffold.dashboard     # one-page board → http://127.0.0.1:8099
python -m scaffold.publisher.app # the thin publisher (row feed + Lane A board/submit + arena intake)
```

Release-candidate gates (all must be green before shipping the wire surface):

```bash
python rc_verify.py          # scoring invariants across all lanes
python wire_compat.py        # byte-compat of our signing vs live production rows (8/8)
python publisher_verify.py   # end-to-end miner sign → fetch → solve → submit → validator-pull
```

## Deploy / go-live

The thin publisher takes over `api.cathedral.computer` with **zero validator
updates** via a same-URL Railway swap. Full step-by-step (stage → seed → soak →
swap → rollback/abort criteria) in [`deploy/RUNBOOK.md`](deploy/RUNBOOK.md).
Current status and the gates to a miners-paid deployment: [`RELEASE_STATUS.md`](RELEASE_STATUS.md).

## Layout

- `scaffold/lanes/` — the lanes (`solver_arena`, `sat_challenge`, `encoding`) + the z3 encoder
- `scaffold/contract.py` — the Lane contract (pure generate / verify / score)
- `scaffold/grading.py`, `timing.py` — shared scoring + server-measured speed
- `scaffold/consensus.py` — cross-miner counterexample-beats-majority refutation
- `scaffold/verify.py`, `polaris.py` — witness/DRAT-cert + TDX attestation checks
- `scaffold/wire.py` — byte-faithful v5/v6 signed-row port (the frozen feed surface)
- `scaffold/publisher/` — thin publisher (app, feed, board/submit, arena intake, seed_live, refill, soak)
- `deploy/` — Dockerfile, railway.toml, RUNBOOK
- `fixtures/live-20260609/` — golden vectors from the live network
