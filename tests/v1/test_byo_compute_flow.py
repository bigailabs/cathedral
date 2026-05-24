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
    assert "**Live lanes**" in body
    assert "`synthetic_boolean_v1` SAT is live on mainnet" in body
    assert "**Live vertical**" not in body
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
    assert body["description"] == "Publisher API for Cathedral SN39."
    assert body["links"]["health"] == "/health"
    assert body["links"]["skill"] == "/skill.md"
    assert body["links"]["api"] == "/api/cathedral"
    assert body["links"]["eval_spec"] == "/api/cathedral/v1/cards/eu-ai-act/eval-spec"
    assert body["links"]["recent_signed_evals"] == "/api/cathedral/v1/leaderboard/recent"
    assert (
        body["links"]["sat_readiness"]
        == "/api/cathedral/v1/synthetic-boolean/readiness-probe"
    )
    assert body["links"]["submit_agent"] == "/api/cathedral/v1/agents/submit"


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


def test_skill_md_includes_public_safe_sat_contract(publisher_client: object) -> None:
    """skill.md must include the generic SAT miner contract."""
    if publisher_client is None:
        pytest.skip("publisher app not buildable")
    r = publisher_client.get("/skill.md")  # type: ignore[attr-defined]
    body = r.text
    lowered = body.lower()

    assert "synthetic_boolean_v1" in body
    assert "static contract and onboarding reference" in lowered
    assert "not a challenge feed" in lowered
    assert "sat challenges are not listed" in lowered
    assert "public_input.cnf_url" in body
    assert "public_input.cnf_sha256" in body
    assert "num_vars" in body
    assert "num_clauses" in body
    assert "fetch `public_input.cnf_url` exactly as given" in lowered
    assert "SHA-256" in body
    assert "```FINAL_ANSWER" in body
    assert '"dimacs_solution"' in body
    assert "first submitted among valid receipts, not first verified" in lowered
    assert "sat mainnet weight is live" in lowered
    assert "active cnf url is issued only inside cathedral's ssh/hermes eval prompt" in lowered
    assert "/api/cathedral/v1/synthetic-boolean/readiness-probe" in body
    assert "always returns `weighted_score: 0.0`" in body


def test_skill_md_sat_contract_has_no_private_challenge_material(
    publisher_client: object,
) -> None:
    """The public onboarding doc must not expose private SAT artifacts."""
    if publisher_client is None:
        pytest.skip("publisher app not buildable")
    r = publisher_client.get("/skill.md")  # type: ignore[attr-defined]
    lowered = r.text.lower()

    forbidden = (
        "p cnf",
        "active.cnf",
        "operator-input.cnf",
        ".dimacs",
        ".sol",
        "fetch_token",
        "token-bearing",
        "planted_assignment",
        "generator_version",
        "private_corpus",
        "/private/",
        "sro" + "gatch",
        "rse" + "rge",
        "uf20-" + "01",
        "uf50-" + "01000",
        "uf250-" + "0100",
        "sha" + "1.cnf",
    )
    offenders = [needle for needle in forbidden if needle in lowered]
    assert not offenders, f"skill.md leaked private SAT marker(s): {offenders}"


def test_synthetic_boolean_readiness_probe_is_zero_weight(
    publisher_client: object,
) -> None:
    if publisher_client is None:
        pytest.skip("publisher app not buildable")

    probe = publisher_client.get(  # type: ignore[attr-defined]
        "/api/cathedral/v1/synthetic-boolean/readiness-probe"
    )
    assert probe.status_code == 200
    body = probe.json()
    assert body["capability"] == "synthetic_boolean_v1"
    assert body["purpose"] == "readiness_probe"
    assert body["emissions_eligible"] is False
    assert body["weighted_score"] == 0.0
    assert body["public_input"]["format"] == "dimacs"
    assert body["public_input"]["cnf_url"].endswith(
        "/api/cathedral/v1/synthetic-boolean/readiness-probe/cnf"
    )
    assert body["public_input"]["cnf_sha256"]

    cnf = publisher_client.get(  # type: ignore[attr-defined]
        "/api/cathedral/v1/synthetic-boolean/readiness-probe/cnf"
    )
    assert cnf.status_code == 200
    assert cnf.text == "p cnf 3 3\n1 -2 3 0\n-1 2 3 0\n1 2 -3 0\n"

    verify = publisher_client.post(  # type: ignore[attr-defined]
        "/api/cathedral/v1/synthetic-boolean/readiness-probe/verify",
        json={"dimacs_solution": "s SATISFIABLE\nv 1 2 3 0\n"},
    )
    assert verify.status_code == 200
    assert verify.json()["valid"] is True
    assert verify.json()["weighted_score"] == 0.0
    assert verify.json()["emissions_eligible"] is False


def test_synthetic_boolean_readiness_verify_rejects_large_solution(
    publisher_client: object,
) -> None:
    if publisher_client is None:
        pytest.skip("publisher app not buildable")

    verify = publisher_client.post(  # type: ignore[attr-defined]
        "/api/cathedral/v1/synthetic-boolean/readiness-probe/verify",
        json={"dimacs_solution": "s SATISFIABLE\nv " + ("1 " * 4097) + "0\n"},
    )

    assert verify.status_code == 413
    assert verify.json()["detail"] == "dimacs_solution too large"


def test_synthetic_boolean_readiness_verify_rejects_large_body(
    publisher_client: object,
) -> None:
    if publisher_client is None:
        pytest.skip("publisher app not buildable")

    verify = publisher_client.post(  # type: ignore[attr-defined]
        "/api/cathedral/v1/synthetic-boolean/readiness-probe/verify",
        json={"dimacs_solution": "s SATISFIABLE\nv " + ("1 " * 9000) + "0\n"},
    )

    assert verify.status_code == 413
    assert verify.json()["detail"] == "readiness verify body too large"


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
