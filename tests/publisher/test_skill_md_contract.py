from __future__ import annotations

from cathedral.publisher.skill_md import SKILL_MD_CONTENT


def test_skill_md_is_sat_first_and_actionable() -> None:
    assert SKILL_MD_CONTENT.startswith("# Cathedral SAT miner contract")
    assert "`synthetic_boolean_v1`" in SKILL_MD_CONTENT
    assert "public_input.cnf_url" in SKILL_MD_CONTENT
    assert "FINAL_ANSWER" in SKILL_MD_CONTENT
    assert "dimacs_solution" in SKILL_MD_CONTENT
    assert "active-challenges" in SKILL_MD_CONTENT
    assert "Common rejection reasons" in SKILL_MD_CONTENT
    assert "Starter solvers" in SKILL_MD_CONTENT
    assert len(SKILL_MD_CONTENT) < 12000


def test_skill_md_excludes_retired_lane_material() -> None:
    banned = (
        "EU AI Act",
        "Card schema",
        "no_legal_advice",
        "bug_isolation_v1",
        "full Hermes package",
        "Nitro",
        "TDX",
        "SEV-SNP",
        "cathedral-baseline-agent",
        "Live vertical",
        "mine a card",
    )
    present = [needle for needle in banned if needle in SKILL_MD_CONTENT]
    assert not present


def test_skill_md_warns_readiness_probe_is_not_competition() -> None:
    lowered = SKILL_MD_CONTENT.lower()
    assert "readiness probe is a toy smoke test only" in lowered
    assert "probe is not the competition" in lowered
    assert "do not treat it as the live challenge feed" in lowered
    assert "weighted_score: 0.0" in SKILL_MD_CONTENT
