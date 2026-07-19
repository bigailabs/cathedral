# Independent Fable Review Record

Date: 2026-07-18  
Artifact: Cathedral thin-subnet release candidate  
Mode: fresh, non-persistent, read-only Fable sessions

## Scope and boundary

The user explicitly authorized Fable to review the release-candidate subnet.
The review covered `cathedral_thin/`, `tests/thin/`, the thin deployment and
policy examples, and the thin design, evidence, and runbook documents. It
excluded unrelated repository code, `cathedralai/cathedral`, credentials, live
wallets, chain writes, deployments, edits, and internet access.

The review question was whether the implementation is correct, internally
coherent, operationally dependable, decentralized, thin enough to avoid a
central hosted data plane, resistant to incentive gaming, deterministic in
scoring and weight composition, robust across registration and retry behavior,
and ready for a realistic testnet exercise.

## Initial verdict

Fable reported **no blocking findings**. It specifically verified challenge
and witness binding, coldkey collapse, signed score-class policy enforcement,
owner-registration authority, replay and equivocation checkpoints,
pending-vector digest binding, authority-change cancellation, deterministic
composition, validator sovereignty, and the absence of an owner data plane.

Fable retained these actionable findings:

1. Periodic miner permit refresh could terminate the process on a transient
   chain-RPC failure.
2. The continuous validator service loop caught only `ThinSubnetError`, so a
   raw SDK exception could terminate the process.
3. An already ambiguous pending vector could lose its ambiguity flag if an
   opted-in retry failed during identity or owner-registration refresh before
   any new submission.
4. The runbook did not state the miner/validator clock-synchronization
   requirement or its observable failure reasons.

An additional dependency concern about `httpx2` was disproven against current
official PyPI metadata: the package is owned by Pydantic, uses trusted
publishing, carries provenance to `pydantic/httpx2`, and was already present on
the base branch. No dependency change was made from stale reviewer knowledge.

## Remediation

- Miner refresh now installs a permit snapshot atomically and preserves the
  last complete snapshot during a transient refresh failure. Initial startup
  remains strict when no snapshot exists.
- Continuous validator mode now logs raw tick exceptions, retains persisted
  state, backs off, and retries. `--once` still exits nonzero, while cancellation
  and keyboard interrupts still propagate normally.
- Both pre-submission refresh failure paths preserve the existing ambiguity
  flag until an actual submission outcome or an explicit authority-change
  cancellation resolves it.
- The runbook now requires synchronized clocks and alerts on clock-skew and
  stale-permit-snapshot symptoms.
- Five focused regression tests cover the reviewed remediation. Three
  operational regression tests added after the live SN39 dry-run prove the SDK
  Dendrite session helper and `async_main` wiring close cleanly on normal return
  and on immediate failure after client creation.

## Follow-up verdict

The second fresh Fable session reviewed only the remediation and its tests. It
returned: **remediation accepted; all four fixes address the exact failure
paths; no blocking finding remains**.

After the SN39 dry-run exposed an SDK client-session cleanup warning, two more
fresh read-only Fable passes reviewed the explicit Dendrite close. The first
accepted the cleanup but requested direct `async_main` coverage and removal of
a small pre-`try` exception window. Both were corrected. The follow-up returned:
**accepted; both findings resolved; cleanup is correct on success, fail-closed
error, and cancellation; no blocker remains**.

The follow-up retained four non-blocking observations:

- a prolonged chain-RPC outage can leave the miner using a stale permit
  snapshot, so repeated warnings must be monitored;
- the documented 30-second clock tolerance must remain aligned with the code;
- an exception inside a narrow mutation/save window can leave in-memory state
  ahead of disk, but the constructed outcome holds the prior on-chain vector;
- owner-authorization cancellation uses string-prefix classification, whose
  safe failure mode is a hold but which could become typed exceptions later.

## Remaining evidence gates

Fable did not claim that the subnet is uncheatable or mainnet-ready. The
remaining gates are a real two-process Axon/Dendrite testnet exercise, an
operator-authorized weight broadcast, a live third-party owner-registration and
rotation drill, compatible target-netuid weight hyperparameters, an ambiguous
commit-reveal outcome drill, and a real Cathedral Confidential producer for the
new signed score-class report.
