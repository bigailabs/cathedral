# Cathedral — intelligence in practice

> Take a real smart-contract property, compile it to logic, and **prove** it with
> a solver — inside a fortress no cheat escapes.

Cathedral is a subnet that pays weight for one thing only: **work it can
independently verify**. It turns verification problems into a market — encode a
property, race to solve it, prove the answer — and routes reward to verified
artifacts and nothing else.

## The fortress: three corners, three lanes

```
                 ┌─ ENCODE (C) ─┐                a property becomes a must-solve problem
                 │              │
        verify ──┤    VAULT     ├── attest        the walls are the checks:
                 │  (paid only  │                   verify the artifact
                 │   verified)  │                   attest the run
        SOLVE (A)└── consensus ──┘ IMPROVE (B)       cross-miner consensus
```

- **Encode (Lane C)** — encodes the **Bittensor Substrate↔EVM balance bridge**:
  when TAO crosses between the 9-decimal Substrate side and the 18-decimal EVM
  side, value must be conserved (`intoSubstrate(intoEvm(s)) == s`). A miner
  compiles this to SMT and **solves for an input that breaks it** — a real
  mint/burn bug. The fault fires only behind a solve-hard mixing trigger, so a
  guessed constant earns nothing: you must actually solve.
- **Solve (Lane A)** — fastest valid SAT witness wins; the witness self-verifies
  against the formula (zero trust); speed is **server-measured**, never the
  miner's word.
- **Improve (Lane B)** — attested solve. A verified solve earns a correctness
  floor; the top of the range is unlocked only by an **attested, tamper-evident
  elapsed** bound into a TDX quote. "Faster" is provable, not self-reported.

The **vault** pays only artifacts that survive the walls. The **seal** tracks
escapees — verified as of now: `0`.

## Why it's trustworthy (the checks & balances)

| Attack | Caught by |
|---|---|
| Submit a fake/guessed witness | the verifier re-checks the artifact (formula / counterexample / cert) |
| Claim "no bug / safe" to dodge work | equivalence gate + windowed traps + cross-miner consensus (a peer's verified find refutes you) |
| Self-report a fast time | speed is server-measured (A/C) or hardware-attested (B) — the miner's `wall_ms` is ignored |
| Tamper with an attested elapsed | the quote binding (`report_data` = image‖stdout) breaks → rejected |

No positive weight is ever paid for an unverifiable claim.

## Run it

```bash
python -m scaffold.live          # the validator runner (real cryptominisat / drat-trim / z3 when present)
python -m scaffold.dashboard     # the one-page board → http://127.0.0.1:8099
python rc_verify.py              # release-candidate gate: every invariant across all 3 lanes
```

## Status

Release-candidate: the scoring mechanism is built and verified (`rc_verify.py`,
12/12 checks). See [`RELEASE_STATUS.md`](RELEASE_STATUS.md) for what's proven and
the gates to a live, miners-paid deployment (the top one being the live-SN39
emission path, not this scaffold).

## Layout

- `scaffold/lanes/` — the three lanes (`encoding`, `sat_challenge`, `solver_docker`) + the real z3 encoder
- `scaffold/contract.py` — the Lane contract (pure generate / verify / score)
- `scaffold/grading.py`, `timing.py` — shared scoring + server-measured speed
- `scaffold/consensus.py` — cross-miner refutation
- `scaffold/verify.py`, `polaris.py` — UNSAT-cert + TDX attestation checks
- `scaffold/specimen.py`, `dashboard.py` — the worked examples + the fortress board
