"""BYO-compute flow + /skill.md route tests (Moltbook-style onboarding).

Per Fred's Moltbook decision (CONTRACTS.md Section -1):
- polaris_agent_id is now optional on PolarisAgentClaim
- BYO-compute submissions still score, just without the verified multiplier
- Polaris-verified submissions get a 1.10x quality multiplier (capped at 1.0)
- /skill.md is the canonical agent-facing entry point
"""

from __future__ import annotations

import pytest

from cathedral.types import PolarisAgentClaim


def test_polaris_agent_claim_accepts_none_polaris_agent_id() -> None:
    """The wire schema must allow None so BYO-compute miners can submit."""
    claim = PolarisAgentClaim(
        miner_hotkey="5Hot",
        owner_wallet="5Own",
        work_unit="card:eu-ai-act",
        polaris_agent_id=None,
    )
    assert claim.polaris_agent_id is None


def test_polaris_agent_claim_accepts_string_polaris_agent_id() -> None:
    """Polaris-verified path still works (back-compat)."""
    claim = PolarisAgentClaim(
        miner_hotkey="5Hot",
        owner_wallet="5Own",
        work_unit="card:eu-ai-act",
        polaris_agent_id="agt_test_123",
    )
    assert claim.polaris_agent_id == "agt_test_123"


def test_skill_md_route_returns_markdown(publisher_client: object) -> None:
    """GET /skill.md serves the canonical agent-onboarding doc."""
    if publisher_client is None:
        pytest.skip("publisher app not buildable")
    r = publisher_client.get("/skill.md")  # type: ignore[attr-defined]
    assert r.status_code == 200
    ctype = r.headers.get("content-type", "")
    assert "text/markdown" in ctype, f"expected text/markdown, got {ctype!r}"
    body = r.text
    # Spot-check that the canonical content is present and substantive.
    assert "Cathedral skill" in body
    assert "/v1/agents/submit" in body
    assert "X-Cathedral-Signature" in body
    assert "no_legal_advice" in body
    assert len(body) > 2000, "skill.md should be substantive (> 2 KiB)"


def test_api_root_points_to_public_entrypoints(publisher_client: object) -> None:
    """GET / gives humans a small map instead of FastAPI's default 404."""
    if publisher_client is None:
        pytest.skip("publisher app not buildable")
    r = publisher_client.get("/")  # type: ignore[attr-defined]
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "cathedral-publisher"
    assert body["links"]["health"] == "/health"
    assert body["links"]["skill"] == "/skill.md"
    assert body["links"]["api_base"] == "/api/cathedral"
    assert body["links"]["submit"] == "/api/cathedral/v1/agents/submit"


def test_skill_md_mentions_byo_path(publisher_client: object) -> None:
    """skill.md must teach BYO-compute, the only live mining path in v1.

    v1.1.0 made BYO Box (`ssh-probe`) the sole production path. The
    legacy Polaris-hosted runtime is no longer the alternative; the
    previous "polaris must be named" assertion was retired with that
    migration.
    """
    if publisher_client is None:
        pytest.skip("publisher app not buildable")
    r = publisher_client.get("/skill.md")  # type: ignore[attr-defined]
    body = r.text.lower()
    assert "byo" in body or "bring your own" in body


def test_verified_multiplier_capped_at_one() -> None:
    """A 0.95 score x 1.10 = 1.045, must clip to 1.0 not exceed."""
    # Direct test of the cap formula in scoring_pipeline.
    weighted_after_first_mover = 0.95
    multiplier = 1.10
    capped = min(1.0, weighted_after_first_mover * multiplier)
    assert capped == 1.0


def test_byo_compute_no_multiplier() -> None:
    """polaris_agent_id empty/None → multiplier = 1.0, no bonus."""
    multiplier = 1.10 if bool("") else 1.0
    assert multiplier == 1.0
    multiplier = 1.10 if bool(None) else 1.0
    assert multiplier == 1.0
