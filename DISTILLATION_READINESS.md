# Distillation v0 Readiness

## Scope

- Distillation v0 starts from Cathedral audit traces emitted by local replay.
- Raw traces are private by default.
- Public exports are redacted summaries, not raw datasets.
- No live service, earning, disclosure, or training pipeline is implied by this slice.

## Dataset Categories

| Category | Source | Visibility | Value |
| --- | --- | --- | --- |
| `raw_private_audit_trace` | `cathedral.audit_trace.v1` from local audit replay | Private only | Full internal record for operator review and future training curation. |
| `accepted_reproduced_witness` | Accepted trace with deterministic replay evidence | Private training, or public redacted after disclosure gate | Positive example of a witness that reproduced against pinned target logic. |
| `rejected_claim_negative_control` | Rejected trace with verifier stage and reason | Private training | Valuable negative control. Teaches models and evaluators what bad evidence, stale packages, decode failures, and non-reproducing claims look like. |
| `public_redacted_export` | `cathedral.audit_trace.export.v1` with `audience=public` | Public only after gate | Disclosure-safe summary with target identifiers and raw witness/agent data removed. |
| `excluded_sensitive_material` | Raw witness, agent trace, replay artifacts, repo URL, commit, hotkey | Not public | Kept private unless an operator intentionally includes it in a private export. |

## Export Rules

- Use `export_trace(trace)` or `RedactionPolicy(audience="private")` for default private export.
- Public export requires `disclosure_status` to be one of:
  - `fixed`
  - `public`
  - `opt_in`
  - `cathedral_owned`
- Public export strips repo URL, commit, netuid, raw witness, raw agent trace, and replay artifacts.
- Public hashes require a strong redaction secret:
  - use a 128-bit-or-stronger random salt, or
  - use a server-side HMAC secret,
  - never publish the salt or HMAC secret beside the dataset.

## Local Gate

```bash
python3 distillation_verify.py
```

The gate must prove:

- private export is the default,
- public export is disclosure-gated,
- weak public hash secrets are rejected,
- public redaction guidance is present,
- rejected traces are retained as valuable negative controls.
