"""The agent reasoning layer — forming an exploit hypothesis BEFORE encoding.

A real miner-agent does not jump straight to a CNF: it reads the candidate
finding, decides *which invariant family* the weakness belongs to, and states
*what it would prove* (the negated invariant whose SAT model is the exploit
input). That reasoning step is what makes the encode/solve principled rather
than mechanical, and it becomes training data alongside the tool trace.

Two policies, same output shape:

  * `form_hypothesis(target)` — DETERMINISTIC, no spend. Classifies the target's
    candidate finding into the invariant taxonomy (the audit-hunter rulebook),
    picks the concrete encode rule, and writes a grounded rationale. Fully
    testable: every one of the 17 real subnet targets maps to a sensible family.

  * `form_hypothesis_llm(target)` — a GATED drop-in Pi/Hermes-style LLM policy
    (Claude Opus 4.8). It chooses the family + writes the rationale when an
    ANTHROPIC_API_KEY is present, and returns None (caller falls back to the
    deterministic path) when it is not — so the default run never spends. The
    call is structured-output constrained, so the policy can only return a
    family that exists in the rulebook.

`form_hypothesis_best(target)` prefers the LLM when available, else deterministic
— a single entry point the tool loop calls. Like the DCAP-attestation path, the
expensive realness is built and tested, but off by default.
"""
from __future__ import annotations

import json
import os

# -- the invariant taxonomy (the audit-hunter rulebook) -----------------------
# Each family states the invariant that must hold and the concrete rule for
# encoding its NEGATION as a CNF (the SAT model = the exploit-triggering input).
# Keys match models.Target.family / replay cls so the whole stack speaks one
# vocabulary.

RULEBOOK: dict[str, dict[str, str]] = {
    "A_conservation": {
        "invariant": "value conserved: tokens in == tokens out, no value minted or burned by a path",
        "rule": "encode the AMM/split balance equation; assert SAT iff out-in != 0 (fixed-point)",
        "gist": "conservation",
    },
    "B_bounds": {
        "invariant": "arithmetic stays in range: no overflow, underflow, silent-zero, or truncation",
        "rule": "encode the U64F64 fee/price math; assert SAT iff a representable input yields a wrong-magnitude result (e.g. fee rounds to 0)",
        "gist": "bounds",
    },
    "F_emission": {
        "invariant": "emission conserved: rewards sum to the pool, no double-pay or over-emission",
        "rule": "encode the payout/emission split; assert SAT iff total paid > pool or a miner is paid twice",
        "gist": "emission",
    },
    "G_scoring": {
        "invariant": "score reflects real coverage: no undeserved or inflated weight",
        "rule": "encode the scoring/coverage map; assert SAT iff a submission with no/low coverage receives a high score",
        "gist": "scoring",
    },
    "H_trust": {
        "invariant": "proof-of-work & ownership: no replay of a reference artifact, correct owner only",
        "rule": "encode the ownership/nonce binding; assert SAT iff a non-owner or a replayed artifact verifies",
        "gist": "trust",
    },
    "I_safety": {
        "invariant": "no unsafe state: no div-by-zero, panic, or unchecked unwrap on a reachable path",
        "rule": "encode the guard conditions; assert SAT iff a reachable input hits the unsafe branch",
        "gist": "safety",
    },
}

# keyword -> family, checked before falling back to Target.family. Lets the
# hypothesis former refine (e.g. a 'truncation' scoring bug is really B_bounds).
_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("overflow", "underflow", "silent", "truncat", "round", "u64", "fixed-point", "fixed point"), "B_bounds"),
    (("div", "zero", "panic", "unwrap", "unchecked", "assert"), "I_safety"),
    (("emission", "reward", "payout", "double-pay", "double pay", "over-emit", "mint"), "F_emission"),
    (("conservation", "split", "balance", "drain", "value"), "A_conservation"),
    (("replay", "reference", "ownership", "owner", "nonce", "stake-copy", "copy"), "H_trust"),
    (("score", "weight", "coverage", "inflation", "inflat", "rank"), "G_scoring"),
]


def classify(target) -> str:
    """Pick the invariant family for a target's candidate finding. Keyword
    evidence first (more specific than the coarse heat-map family), then the
    target's own family, then a safe default."""
    text = f"{target.candidate_title} {target.exploit_steps} {target.location}".lower()
    for keys, fam in _KEYWORDS:
        if any(k in text for k in keys):
            return fam
    fam = getattr(target, "family", "B_bounds")
    return fam if fam in RULEBOOK else "B_bounds"


def form_hypothesis(target) -> dict:
    """Deterministic exploit hypothesis. Returns a structured object the tool
    loop threads into the receipt and the UI renders:

      family            invariant family (a rulebook key)
      invariant         the property that must hold
      rule              how to encode its negation as a CNF
      expected_property the concrete thing the SAT model would demonstrate
      rationale         grounded, target-specific reasoning (training data)
      source            'deterministic-rulebook'
    """
    fam = classify(target)
    entry = RULEBOOK[fam]
    title = (target.candidate_title or "(unspecified weakness)").strip()
    loc = target.location or target.repo or "the target"
    expected = (f"a representable input to {loc} that violates "
                f"'{entry['gist']}' — its SAT model is the exploit input")
    rationale = (
        f"sn{target.netuid} {target.name}: the candidate finding "
        f"\"{title[:80]}\" at {loc} is a {fam} weakness. "
        f"Invariant that should hold: {entry['invariant']}. "
        f"Proof plan: {entry['rule']}. If the solver finds a model, the decoded "
        f"witness is the exploit-triggering input; the harness then replays it to "
        f"confirm the real invariant is violated (proof, not claim).")
    return {
        "family": fam,
        "invariant": entry["invariant"],
        "rule": entry["rule"],
        "expected_property": expected,
        "rationale": rationale,
        "source": "deterministic-rulebook",
    }


# -- the gated LLM policy (Pi/Hermes-style) -----------------------------------

LLM_MODEL = "claude-opus-4-8"

# structured-output schema: the policy may ONLY return a family in the rulebook.
_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "family": {"type": "string", "enum": sorted(RULEBOOK)},
        "rationale": {"type": "string"},
    },
    "required": ["family", "rationale"],
    "additionalProperties": False,
}


def llm_available() -> bool:
    """True iff a real Claude policy can run: an API key AND the SDK present.
    Off by default on this machine (no key) — the deterministic path is used."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except Exception:
        return False
    return True


def _client():
    import anthropic
    return anthropic.Anthropic()


def _llm_prompt(target) -> str:
    fams = "\n".join(f"  - {k}: {v['invariant']}" for k, v in sorted(RULEBOOK.items()))
    return (
        "You are a Cathedral miner-agent's reasoning policy. Given a subnet "
        "audit target, choose the SINGLE invariant family whose violation best "
        "explains the candidate finding, and write a one-paragraph rationale "
        "describing the invariant that should hold and how its negation would be "
        "encoded as a SAT instance whose model is the exploit input.\n\n"
        f"Target: sn{target.netuid} {target.name}\n"
        f"Repo: {target.repo}\n"
        f"Candidate finding: {target.candidate_title}\n"
        f"Location: {target.location}\n"
        f"Trigger: {target.exploit_steps}\n\n"
        f"Invariant families:\n{fams}\n\n"
        "Return only the structured object.")


def form_hypothesis_llm(target, *, client=None) -> dict | None:
    """LLM policy (Claude Opus 4.8). Returns the same shape as
    `form_hypothesis`, with source 'llm:<model>'. Returns None when no key/SDK is
    available (caller falls back) or on any error — never raises into the round.

    `client` may be injected (a real Anthropic client, or a mock in tests)."""
    if client is None:
        if not llm_available():
            return None
        try:
            client = _client()
        except Exception:
            return None
    try:
        resp = client.messages.create(
            model=LLM_MODEL,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": _LLM_SCHEMA}},
            messages=[{"role": "user", "content": _llm_prompt(target)}],
        )
        # refusal-safe: never index content before checking stop_reason
        if getattr(resp, "stop_reason", None) == "refusal":
            return None
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), None)
        if not text:
            return None
        data = json.loads(text)
    except Exception:
        return None

    fam = data.get("family")
    if fam not in RULEBOOK:
        return None
    base = form_hypothesis(target)         # deterministic scaffold (rule, expected_property)
    base.update({
        "family": fam,
        "invariant": RULEBOOK[fam]["invariant"],
        "rule": RULEBOOK[fam]["rule"],
        "rationale": (data.get("rationale") or base["rationale"]).strip(),
        "source": f"llm:{LLM_MODEL}",
    })
    return base


def form_hypothesis_best(target, *, client=None) -> dict:
    """Single entry point the tool loop calls: the LLM policy when it can run,
    the deterministic rulebook otherwise. Always returns a hypothesis."""
    return form_hypothesis_llm(target, client=client) or form_hypothesis(target)
