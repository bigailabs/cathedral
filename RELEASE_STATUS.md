# Cathedral tripartite — release-candidate status

**One line:** the scoring mechanism is built and verified end-to-end; "miners
actually get paid" is gated on items below, the biggest of which lives in the
*live* SN39 emission path, not in this scaffold.

## What this is
A 3-rail subnet scaffold that pays weight ONLY for independently-verified
artifacts. The rails are the encode → solve → improve flow:

- **Encode (Lane C)** — encodes the real Bittensor Substrate↔EVM balance
  round-trip (TAO: 9-decimal RAO ↔ 18-decimal EVM; value must be conserved) and
  pays a *verified counterexample*. The fault fires only behind a solve-hard
  bit-mixing trigger `low_k(mix(s))==T`, so a guessed constant earns nothing —
  you must run a solver. Score = correctness + speed + rarity.
- **Solve (Lane A)** — fastest valid SAT witness wins; the witness self-verifies
  against the formula; speed is **server-measured**, never miner-reported.
- **Improve (Lane B)** — attested solve. A verified solve earns a correctness
  floor; the top of the range is unlocked only by an **attested, tamper-evident
  elapsed** bound into the TDX quote (`report_data` binds image‖stdout).

## Verified (RC gate — `rc_verify.py`, run on Stitch/z3, all 12 PASS)
- Lane A: witness verifies; liar rejected; self-reported `wall_ms` ignored;
  faster server-time scores higher.
- Lane B: attested ≥ non-attested floor; attesting earns strictly more; timeout
  fraud blocked; **altering the bound elapsed breaks the quote** (rejected).
- Lane C: crier const-`0` never earns (0/145 buggy); real solver earns on all
  buggy (145/145); 0 exploit bypass; 0 crashes on malformed input.
- Overnight: thousands of adversarial attempts, **0 bypass, 0 crashes**;
  codex-reviewed (3 findings fixed); on-chain weight-set proven on testnet.

Reproduce: `python -m scaffold.live` (runner) + `python -m scaffold.dashboard`
(board, http://127.0.0.1:8099); RC gate via `rc_verify.py`.

## Gates before live (in priority order)
1. **Payout / Path-A (the real blocker, lives in live SN39, not here).** Verified
   work is scored perfectly but emission still burns via the score-blind path, so
   a credited miner earns 0 on-chain (observed live: UID 36 has publisher weight
   0.6 yet emission 0; same as our UID 76). No scoring fix pays anyone until the
   signed/multiplied vector drives the dominant weight path. (issue #251.)
2. **Own-miners testnet run** — blocked on the testnet100 coldkey password (not
   yet provided).
3. **Real attestor for Improve** — binding + tamper-evidence are real; wire the
   in-TEE measurement (live `/v1/attest`) so the elapsed is hardware-measured,
   not sim-runner-supplied.
4. Minor: `reference_ms` calibration; longitudinal "solvers improving" series.

## Not gaps (checked)
- Mixing trigger constants (m1,m2,T) already vary per challenge — no fixed
  public function to precompute against; per-epoch rotation is redundant.
