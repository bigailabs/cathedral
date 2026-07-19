# Cathedral Thin Subnet

Status: independently reviewed release candidate; testnet broadcast remains gated  
Scope: a validator-owned VerifyML and federated score-class subnet with no required owner data plane

## Outcome

The subnet's focused utility is portable verification of ML inference: model,
runtime, request, output, and execution evidence are committed in miner-signed
receipts that validators can independently verify and assign into locally
chosen classes. SAT is retained as a bounded hidden integrity canary, not the
market-facing utility. See [`VERIFYML.md`](VERIFYML.md).

The subnet owner hosts no scoring API, challenge service, database, object
store, queue, or solver farm. Miners host SAT solvers. Each Bittensor validator
creates private challenges, sends them directly to registered miners over
Axon/Dendrite, verifies returned witnesses locally, persists a small score
checkpoint, and sets its own weights on Subtensor.

The only owner-operated responsibilities are publishing source releases,
documenting compatible protocol versions, and monitoring the public chain.
Optional external score classes are distributed by the subsystem that produces
their evidence; they do not introduce a central weight setter.

## Architecture

```text
Subtensor metagraph + block height
              |
              v
validator -- private per-miner CNF --> miner Axon --> local SAT solver
    |                                      |
    +<-- compact complete assignment ------+
    |
    +-- verify every clause locally
    +-- update bounded EMA checkpoint
    +-- collapse hotkeys by coldkey
    +-- set_weights on Subtensor

signed class reports from Cathedral Confidential / other components
    +-- source facts, reasons, policy/verifier digests, evidence references
    +-- validator pins keys and chooses class budgets/assignment locally
    +-- fixed-budget composition joins the same validator-owned set_weights path

optional source-subnet owner registration
    +-- current owner coldkey signs class/report-key delegation
    +-- delegate hotkey must remain registered on this target subnet
    +-- validator still chooses admission, allocation, assignment, and evidence
```

There is no owner-controlled service in this path. The work scales horizontally
with the validators that already earn validator emissions. At the default
maximum subnet size, one validator performs one bounded RPC and one linear-time
witness check per miner per scoring round.

## Protocol

One `SatChallenge` synapse carries:

- protocol version and unique challenge ID;
- validator and target miner hotkeys;
- round number, issue time, and hard expiry;
- variable/clause counts and a compressed DIMACS CNF;
- an empty response assignment on request and a packed bitset assignment on
  response.

The validator derives a 256-bit per-miner seed with HMAC-SHA256 over its local
master secret and `(netuid, validator hotkey, miner hotkey, round, slot)`. A
cryptographic counter-mode PRNG generates an AJM two-hidden-assignment 3-SAT
formula near the phase transition. The seed and planted witnesses never cross
the wire. The validator does not score against the planted witness; it checks
the miner's complete assignment against every clause.

`challenge_id` commits to the protocol version, identities, round, expiry,
dimensions, and CNF digest. The Bittensor body hash also binds the returned
assignment, solver label, timing metadata, and error. Returned CNF fields are
ignored; the validator grades against its original request object and local
monotonic elapsed time.

## Deterministic reward rule

For miner `i` in round `t`:

```text
valid(i,t) = 1 iff identity, challenge ID, expiry, payload bounds, and every
                 CNF clause verify; otherwise 0

speed(i,t) = reference_ms / (reference_ms + observed_rpc_ms)
round(i,t) = valid(i,t) * (0.80 + 0.20 * speed(i,t))
ema(i,t)   = alpha * round(i,t) + (1-alpha) * ema(i,t-1)
eligible(i,t) = ema(i,t) iff round(i,t) > 0, otherwise 0
```

The 80% correctness component reduces geographic latency advantage while the
20% speed component still rewards better solvers. EMA ranks miners that
verified in the current round; it never pays a miner for historical work alone.
A timeout, malformed answer, wrong witness, replay, identity mismatch, or
transport error has round score zero. Miner-reported timing and solver labels
have no scoring effect.

Before normalization, hotkeys are grouped by their current metagraph coldkey.
Each coldkey's budget is the maximum positive member score, then that budget is
split among its member hotkeys in proportion to their scores. Adding hotkeys
under one coldkey therefore cannot increase that coldkey's total weight.

If there is no positive score, the validator does not submit a new vector. It
never invents a winner or silently routes emissions to the owner.

## Federated score classes

Validators may replace the implicit 100% local-SAT policy with a canonical
local policy containing one local SAT class and/or independently signed
external classes. Allocations must sum exactly to one. External sources submit
facts and provenance, never chain weights: the validator verifies source keys,
network/netuid, freshness, block scope, report chain, completeness, reason
codes, and evidence references, then applies its locally chosen metric,
cap/transform, coldkey collapse, normalization, and allocation.

A configured class that cannot produce a valid positive distribution holds the
entire new round. The implementation never renormalizes the missing budget into
another class. Multiple untrusted mirrors are allowed because the artifact is
signed; same-epoch disagreement is treated as source equivocation. See
[`THIN_SCORE_CLASSES.md`](THIN_SCORE_CLASSES.md) for the exact contract and
Cathedral Confidential adapter requirements.

An external source can alternatively be admitted by source-subnet ownership.
The validator's local policy pins the source netuid and allowed class. Each
round it confirms the current on-chain owner, confirms that the owner has a
delegate hotkey registered under the same coldkey on this subnet, verifies the
owner's signed delegation artifact, and only then accepts reports signed by the
delegated Ed25519 key. The artifact's report URLs must exactly match the
validator's locally pinned HTTPS mirrors, preventing a contributor from using
the validator as an arbitrary fetch proxy. Registration grants no validator
wallet access and no right to submit an opaque final vector.

## State and failure recovery

The validator keeps one small JSON checkpoint containing:

- a generated 32-byte master secret;
- last completed round and per-hotkey EMA scores;
- last attempted and last confirmed vector digests;
- protocol/config fingerprint;
- external class source-epoch/report-ID high-water marks;
- owner-registration owner/delegate/sequence/registration-ID high-water marks;
- the provenance digest for pending and confirmed vectors.

Writes use mode `0600`, temporary-file plus atomic replace, and a process lease
that prevents two validators sharing one checkpoint. A corrupt checkpoint
fails closed instead of resetting scores or the challenge secret. An explicitly
rejected weight vector uses bounded automatic retry. A transport exception
after submission is marked ambiguous and requires explicit operator opt-in
before retry, because commit-reveal commitments are not assumed to be
extrinsically idempotent. A successful response records the confirmed digest
before the next round.

## Weight setting

Each round refreshes the metagraph, discards deregistered identities, resolves
current hotkeys/coldkeys/UIDs, and normalizes finite non-negative weights. The
chain adapter submits with inclusion confirmation and treats tuple, boolean,
and current `ExtrinsicResponse` return shapes explicitly. Failed submissions
remain pending. Definitive rejections retry with bounded exponential backoff;
ambiguous outcomes remain held for operator reconciliation.

Every pending vector stores and hashes its UID-to-hotkey mapping. Immediately
before a retry, the validator refreshes the metagraph and cancels the vector if
any UID now belongs to a different hotkey. This prevents registration churn
from transferring an old reward to a replacement identity.

For owner-registered classes, the pending digest also binds every accepted
registration ID. Before each initial submission or retry, the validator
rechecks source ownership, the delegate hotkey/coldkey pair, registration
freshness, and the signed artifact. Ownership transfer or delegate
deregistration cancels the pending vector; key/registration rotation yields a
different ID and also cancels it. A transient mirror failure holds the vector
without submitting it.

Subnet operators should enable Bittensor's native commit-reveal setting. That
reduces validator weight copying; the runtime continues to call the supported
SDK weight API so the chain's configured weight mode remains authoritative.

## Threat model

| Threat | Defense | Residual risk |
|---|---|---|
| Copy another miner's answer | Every miner receives a different secret-seeded formula bound to its hotkey | A miner can outsource its own live solve; the commodity is the correct solve, not proof of hardware ownership |
| Replay an old answer | Round, expiry, identities, CNF digest, and challenge ID are bound; only the in-flight request is accepted | A compromised validator can rescore data it controls |
| Guess the planted assignment | AJM removes literal-frequency bias; HMAC seed and cryptographic PRNG stay validator-local | Generator or PRNG defects can create exploitable structure |
| Submit malformed/partial/oversized data | Strict bitset length, decompression limit, identity checks, and complete clause verification | Network-level volumetric attacks still need ordinary host/firewall controls |
| Permitted validator floods a miner | Per-validator request window, bounded solver concurrency, one distinct CNF digest per declared round, and cached semantic retries | A malicious permitted validator can vary round IDs; the rate limit and ordinary network controls remain the hard bounds |
| Lie about speed or solver | Validator uses monotonic RPC time and ignores miner timing metadata | Geography and validator load add noise, capped to 20% of a score |
| Sybil hotkeys | Coldkey-level maximum budget before hotkey split; registration cost remains | Separate funded coldkeys cannot be cryptographically linked |
| Miner collusion | Per-miner formulas make answer sharing useless | Live outsourcing of each distinct task cannot be distinguished from operating a distributed solver |
| Solve once, then coast on EMA | Current-round verification gates eligibility; historical EMA only ranks currently verified miners | Intermittent miners can resume their ranking history after producing new valid work |
| Validator favors a miner | Independent validators generate and grade their own challenges; Yuma aggregates stake-weighted views | A stake-majority validator cartel can manipulate any subnet; this design cannot defeat base-consensus capture |
| Validator copies weights | Native Bittensor commit-reveal should be enabled | Chain configuration and validator upgrade discipline remain operational dependencies |
| Weight RPC fails or response is ambiguous | Inclusion/finalization wait, response-shape checks, persisted vector plus UID-hotkey binding; explicit failures retry, unknown outcomes require operator opt-in | A prolonged chain partition leaves the previous on-chain vector active; the operator may need to reconcile a commit-reveal submission |
| Restart loses state | Atomic bounded checkpoint includes secret, scores, and vector digests | Loss of the checkpoint intentionally requires operator recovery rather than silent reset |
| Owner infrastructure outage | No owner data plane exists | Source distribution and package registries can still be unavailable; pinned releases remain runnable |
| External source submits a favored vector | Sources cannot submit vectors; validator policy selects a metric and class budget, and every input carries reason/evidence provenance | A pinned source can lie about a metric unless the validator independently verifies the referenced domain evidence |
| External report replay or rollback | Short time/block windows plus per-class source-epoch/report-ID checkpoints | A validator restored without its checkpoint must recover the checkpoint or explicitly re-bootstrap trust |
| External source equivocation | Same epoch/different report fails; configured mirrors are compared | Different validators can see different fresh epochs during propagation; deterministic local checkpoints and audit records expose the split |
| False source-subnet owner registration | Current source owner coldkey, owner signature, source/target netuids, and target delegate coldkey pair are checked every round | Validators still choose which source netuids to admit; an admitted owner can lie within its bounded class |
| Ownership transfer or delegate deregistration | Old-owner signatures or a missing/moved target delegate hold the entire update; a new on-chain owner becomes a fresh registration root | Chain reorgs and RPC inconsistency can temporarily hold updates; prior on-chain weights remain active |
| Ownership changes while a weight retry is pending | Pending digest binds registration IDs; owner and delegate are rechecked before every attempt, and authority change cancels the stale vector | A transient source or chain outage holds rather than submits the pending vector |
| Contributor tries to seize weight authority | Registration delegates report keys and class IDs only; validator-local policy fixes allocation and assignment and the validator alone signs `set_weights` | Validator operators can voluntarily configure an unsafe allocation or asserted-score rule |
| Contributor supplies an internal or attacker-chosen report URL | Registered classes fetch only exact credential-free HTTPS URLs pinned in validator-local policy; redirects are refused | Validators must still review the DNS and operational security of endpoints they explicitly admit |
| External class outage | Entire new vector is held; its budget is not donated to another class | Prior on-chain weights remain active during a prolonged outage |
| Score-key compromise | Validators pin roots locally and remove/rotate compromised keys | False source facts can be signed until independent validators update their policy |
| Provenance denial or tampering | Canonical signed report plus immutable validator decision record; pending vector binds the decision digest | Evidence URIs can disappear; operators must retain exact evidence bytes for their claimed audit window |

## Explicit non-claims

- The subnet is not literally uncheatable.
- It does not prove which machine performed a solve.
- It does not prevent a majority of validator stake from colluding.
- It does not make hidden identities behind separate coldkeys linkable.
- SAT witnesses prove correctness, not socially useful demand by themselves.
- A signed metric report is not proof that the source measured the metric
  honestly; evidence references make the claim attributable and auditable.

These are residual assumptions to monitor, not reasons to weaken the
self-verifying core.

## Acceptance criteria

1. An honest reference miner solves and receives positive weight end to end.
2. Wrong, incomplete, contradictory, oversized, expired, replayed, copied, and
   identity-swapped answers receive zero.
3. Duplicating a hotkey under one coldkey does not increase coldkey weight.
4. Miner-reported timing cannot change a score.
5. A validator restart preserves secret, EMA, and pending/confirmed vector
   state; a corrupt checkpoint fails closed.
6. Definitive weight submission failures retry the identical vector; ambiguous
   outcomes hold for explicit operator action, and neither advances the
   confirmed digest.
7. The Bittensor runtime imports against the pinned supported SDK and accepts
   current chain response shapes.
8. A local multi-miner simulation exercises generation, transport-shaped
   request/response, verification, scoring, recovery, and fake-chain weight
   submission without any external service.
9. Operator documentation covers subnet creation, registration, miner and
   validator deployment, commit-reveal, dry-run, upgrade, and rollback.
10. The implementation and tests received an independent Fable review. The
    retained findings were remediated, and the follow-up accepted the changes
    with no blocking finding remaining.
11. Signed external classes reject bad keys, tampering, stale/future/block-
    invalid reports, rollback, equivocation, missing evidence, and implicit
    budget reallocation.
12. Every composed vector has an immutable decision record containing the
    local class policy, per-miner reasons/evidence, metagraph mapping, final UID
    vector, and the digest bound into retry state.
