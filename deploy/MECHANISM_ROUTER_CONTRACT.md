# Mechanism Router — shared build contract (authoritative)

All agents build to THIS interface exactly. Do not rename functions, change
signatures, or invent alternate shapes. Repo: /mnt/c/Users/fred/code/cathedral,
branch feat/solution-manifest-v2. Python 3.11, `.venv/bin/python`, tests via
`PYTHONPATH=. .venv/bin/python -m pytest`.

## The thesis this serves
Cathedral is a Verified Artifact Engine: `issued → verified → scored → trained`;
it rewards proof, not claims. The router is the **scored → weights** stage,
generalized so every proof rail (SAT now; Hermes/Secure-Compute later) is a
*mechanism*. One allocation table sets how much weight each mechanism gets;
changing the mix is one edit. Testnet first (dogfood). Nothing here may affect
mainnet/finney weights.

## Core module: `scaffold/publisher/mechanism_router.py`

```python
from dataclasses import dataclass
from typing import Protocol

# uid -> nonnegative score
ScoreVector = dict[int, float]

@dataclass(frozen=True)
class MechanismSpec:
    mechanism_id: str          # unique slug
    owner_pubkey: str          # ss58; the key allowed to post this mechanism's scores
    weight_fraction: float     # 0..1 — THE ONE KNOB
    tier: str                  # "signed" (claims) | "artifact" (proof-backed)
    owner_uid: int | None = None  # UID controlled by the owner, for self-weight blocking
    enabled: bool = True

@dataclass(frozen=True)
class ScoreVectorMeta:
    mechanism_id: str
    signed_at_ms: int
    sig_ok: bool
    source: str                # "signed_post" | "sat_adapter" | ...

class MechanismStore(Protocol):
    def list_specs(self) -> list[MechanismSpec]: ...
    def get_spec(self, mechanism_id: str) -> MechanismSpec | None: ...
    def upsert_spec(self, spec: MechanismSpec) -> None: ...
    def put_scores(self, mechanism_id: str, scores: ScoreVector, meta: ScoreVectorMeta) -> None: ...
    # returns latest (scores, meta) or None; caller decides staleness from meta.signed_at_ms
    def get_scores(self, mechanism_id: str) -> tuple[ScoreVector, ScoreVectorMeta] | None: ...

def compose(
    specs: list[MechanismSpec],
    scores: dict[str, tuple[ScoreVector, ScoreVectorMeta]],
    *,
    registered_uids: set[int],
    block_self_weight: bool = True,
    max_score_age_ms: int | None = None,
    now_ms: int,
) -> tuple[dict[int, float], dict]:
    """Return (final_uid_weights_summing_to_1_or_empty, debug_metadata).

    Algorithm (deterministic, no unseeded randomness):
      - consider only enabled specs with weight_fraction > 0
      - for each mechanism: fetch its (scores, meta). If missing / sig_ok False /
        empty after filtering / (max_score_age_ms set and older than it) →
        that mechanism CONTRIBUTES 0 (record fallback_reason), others unaffected.
      - drop uids not in registered_uids
      - if block_self_weight and spec.owner_uid is not None: force that mechanism's
        score on its OWN owner_uid to 0 (pay miners, not yourself)
      - normalize each surviving mechanism vector to sum 1
      - combined[uid] = Σ (fraction_i * normalized_i[uid])
      - renormalize combined to sum 1; if total is 0 → return ({}, meta) and the
        CALLER keeps the pure V1 vector (never zero a miner, never crash).
      - debug_metadata includes per-mechanism {fraction, contributing, n_uids,
        fallback_reason|null} for auditability.
    """
```

## Allocation reload (the "one edit" knob)
Specs (incl. `weight_fraction`) load from `MechanismStore` (a table), refreshed
each weight cycle. An admin endpoint `PUT /mechanisms/{id}` (admin-token gated)
upserts a spec so the mix changes live without redeploy. Env default: **no
mechanism enabled / all fractions 0** unless explicitly set.

## Isolation / safety (every component)
- Default OFF: with no configured mechanisms, real weight output is byte-identical to today's V1-only vector.
- Testnet only. Any chain interaction is DRY-RUN by default and hard-refuses `network=="finney"` / mainnet netuid.
- Do not modify the V1 scoring path; the router is additive and composes V1 as an implicit mechanism only when a fraction is assigned.
- No secrets in code/logs. Add tests. Keep existing suites green with everything unset.
