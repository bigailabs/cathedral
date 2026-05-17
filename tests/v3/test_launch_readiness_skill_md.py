"""Regression tests pinning the v3 launch-readiness language in skill.md.

These guard the miner-facing contract for `bug_isolation_v1` data
collection:

- Miners must be told the full Hermes package is required for v3 reward
  eligibility (when enabled).
- The package scope must be communicated as task-scoped, not arbitrary
  host scraping.
- The disclaimer must enumerate the categories of host state the package
  does NOT authorize (wallets, SSH private keys, provider API keys,
  `.env` files, arbitrary host state).
- The doc must not promise v3 payouts today. v3 emissions are zero on
  mainnet during this milestone; the only legitimate framing is
  "eligibility when enabled".

If any of these fail, do not silently rewrite the test; check whether
the v3 launch posture really changed and update
`docs/v3/launch-readiness.md` + `docs/v3/mainnet-launch-rule.md` first.
"""

from __future__ import annotations

from cathedral.publisher.skill_md import SKILL_MD_CONTENT


def test_skill_md_mentions_full_hermes_package() -> None:
    assert "full Hermes package" in SKILL_MD_CONTENT, (
        "skill.md must tell miners that v3 reward eligibility requires "
        "the full Hermes package. The exact phrase 'full Hermes package' "
        "is load-bearing: operator docs, the eligibility rule, and the "
        "tests below all key off it."
    )


def test_skill_md_mentions_task_scoped() -> None:
    assert "task-scoped" in SKILL_MD_CONTENT, (
        "skill.md must communicate that the Hermes package is "
        "task-scoped (only what the agent did between prompt and "
        "FINAL_ANSWER), not arbitrary host scraping. The exact phrase "
        "'task-scoped' is load-bearing: it is the boundary miners use "
        "to scope their sandbox."
    )


def test_skill_md_disclaims_arbitrary_host_scraping() -> None:
    required_disclaimers = (
        "arbitrary host",
        "wallets",
        "SSH private keys",
        "provider API keys",
        ".env",
    )
    missing = [s for s in required_disclaimers if s not in SKILL_MD_CONTENT]
    assert not missing, (
        "skill.md must enumerate the host state categories the full "
        f"Hermes package does NOT authorize. Missing: {missing!r}. "
        "Without this enumeration, miners may (reasonably) assume "
        "'full package' includes arbitrary host scraping and refuse to "
        "enable v3."
    )


def test_skill_md_does_not_promise_v3_payouts() -> None:
    # The only acceptable framing is "v3 emissions are zero on mainnet
    # right now; eligibility describes the future state when enabled".
    assert "v3 emissions are currently zero" in SKILL_MD_CONTENT, (
        "skill.md must explicitly say v3 emissions are currently zero "
        "on mainnet, so miners do not infer 'full package' implies "
        "payouts are live."
    )

    # And it must not anywhere promise rewards are live today.
    banned_substrings = (
        "rewards now",
        "rewards today",
        "earning v3 rewards today",
        "v3 rewards are live",
    )
    present = [s for s in banned_substrings if s in SKILL_MD_CONTENT]
    assert not present, (
        f"skill.md must not promise v3 payouts today. Found: {present!r}. "
        "v3 weight on mainnet is 0.0 during this milestone; reward "
        "language must stay in the 'when enabled' eligibility context."
    )
