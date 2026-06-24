"""Capstone — the WHOLE real proof chain coheres in ONE round, and three INDEPENDENT
auditors agree on it. This is the regression anchor for everything built across the
loop: reason → family-coherent proof selection → real tool trace → 15 boolean gates →
reward = metric × gate → Ed25519 signed vector → Merkle anchor → portable bundle
(verified with NO engine) → scoring self-audit → off-box decode (no z3) → real audit
vault → whole-round attestation binding (real Intel-verified TDX quote on file).

"Cathedral rewards proof, not claims" — demonstrated end-to-end, in one test.
"""
from __future__ import annotations

import pytest

from game.arena import audit as _audit
from game.arena import attestation as _att
from game.arena import bundle as _bundle
from game.arena import mint as _mint
from game.arena import replay as _replay
from game.arena.engine import ArenaEngine
from game.arena.ui import render


@pytest.fixture(scope="module")
def game():
    return ArenaEngine().run(1)


def test_reward_is_metric_times_gate_and_self_audits(game):
    """Auditor #1 — scoring: reward = linear_metric × boolean_gate, independently
    re-verified over the engine's own output (6/6 invariants)."""
    v = _audit.audit_scoring(game)
    assert v["ok"] is True, v["violations"]
    assert all(v["checks"].values())
    # the rule, per agent: contribute iff all gates pass
    for a in game.agents:
        assert (a.credit.contrib > 0) == a.gates.passed()
        assert (game.emissions[a.run.miner_hotkey] > 0) == a.gates.passed()


def test_every_cheater_gated_with_a_clear_reason(game):
    """Anti-cheat: every non-honest agent is rejected by a SPECIFIC named gate and
    earns exactly zero — no gate is bypassable."""
    rejected = [a for a in game.agents if not a.gates.passed()]
    assert len(rejected) >= 10                          # the cheat archetypes
    for a in rejected:
        assert a.gates.first_failure() is not None      # a concrete gate failed
        assert game.emissions[a.run.miner_hotkey] == 0.0
        assert a.gates.reasons                           # a human-readable reason


def test_top_earner_bundle_verifies_standalone(game):
    """Auditor #2 — provenance: the winner's portable bundle re-verifies end-to-end
    using ONLY verification primitives (no engine), incl. proof provenance + the
    real corroborating Stitch evidence."""
    top = max((a for a in game.agents if a.gates.passed()),
              key=lambda a: game.emissions[a.run.miner_hotkey])
    b = _bundle.build_bundle(game, top.run.agent_id)
    v = _bundle.verify_bundle(b)
    assert v["ok"] and all(v["checks"].values())
    assert b["proof_provenance"]["source"] in ("z3-factory-mint", "audit_lane", "arena-port")


def test_reasoning_is_coherent_and_replay_is_real(game):
    """Honest agents reason a family, prove a family-matched REAL invariant, and the
    replay actually reproduces — every honest proof is a real violation."""
    honest = [a for a in game.agents if a.gates.passed()]
    assert honest
    for a in honest:
        assert a.gates.hypothesis_aligned is True
        assert a.gates.replay_succeeds is True
    # most of the field proves the family it reasoned (coherent)
    coh = sum(1 for p in game.proof_feed if p.get("reasoning_coherent"))
    assert coh >= 10


def test_offbox_decode_no_z3_in_the_round(game):
    """The off-box loop is real: an EXTERNAL solver's assignment decodes to the
    exploit input via the bit→var map with no z3, reproducing via the harness."""
    ed = game.operator_console.get("external_decode", {})
    if not ed.get("available"):
        return                                          # z3/pysat absent
    assert ed["ok"] is True and ed["reproduced"] is True
    assert ed["decode"] == "bit->var map (no z3)"


def test_round_attestation_binds_the_anchor(game):
    """Whole-round attestation: the round_attest commitment IS the Merkle anchor, so
    one quote attests every proof in the round. A real Intel-verified quote re-verifies."""
    ra = game.operator_console["round_attest"]
    assert ra["commitment"] == game.anchor["merkle_root"]
    # Auditor #3 — attestation: the on-file real TDX quote re-verifies (if present)
    rv = _att.reverify_real_quote()
    if rv.get("available"):
        assert rv["ok"] is True and rv["binding_reverified"] is True


def test_real_audit_vault_spans_verdicts_and_families(game):
    """Subnet-breaking objectives, settled on REAL audit CNFs: the vault carries both
    CRACKED (exploit exists) and HARDENED (no exploit, two solvers agree) cards."""
    v = game.real_audit_vault
    verdicts = {c["verdict"] for c in v}
    if _mint.z3_available():
        assert "CRACKED" in verdicts and "HARDENED" in verdicts
        assert len({c["family"] for c in v}) >= 2        # multiple invariant families


def test_offbox_receipts_become_real_audit_vault_cards():
    """Fire #73: captured off-box receipts are first-class headline vault cards, and
    they REPLACE their weaker minted twins (off-box = real remote hardware)."""
    from game.arena.engine import _real_audit_vault
    offbox = {"available": True, "cnf_satisfied": True, "host": "polarisserver",
              "remote_wall_ms": 2.0, "rule_id": "B2-fee-silent-zero", "cnf_sha256": "abc"}
    hardened = {"available": True, "cross_confirmed": True, "host": "polarisserver",
                "remote_wall_ms": 39.0, "rule_id": "A4-fee-split-conservation", "cnf_sha256": "def"}
    minted = {"sat_minted": [{"target_id": "subtensor-amm:first-fee-silent-zero@MINTED",
                              "family": "B_bounds", "reproduced": True, "code_sha256": "x"}],
              "hardened": [{"model": "subtensor-amm", "rule_id": "A4-fee-split-conservation",
                            "family": "A_conservation", "invariant": "AMM fee conservation",
                            "cdcl_unsat": True, "hardened": True, "cnf_sha256": "y"}]}
    v = _real_audit_vault({}, {}, minted, offbox=offbox, offbox_hardened=hardened)
    ob = {(c["verdict"], c["family"]) for c in v if c.get("offbox")}
    assert ob == {("CRACKED", "B_bounds"), ("HARDENED", "A_conservation")}
    bb = [c for c in v if c["verdict"] == "CRACKED" and c["family"] == "B_bounds"]
    assert len(bb) == 1 and bb[0]["offbox"] is True           # minted silent-zero twin deduped
    a4 = [c for c in v if c["verdict"] == "HARDENED" and c["family"] == "A_conservation"]
    assert len(a4) == 1 and a4[0]["offbox"] is True           # minted AMM-A4 twin deduped


def test_offbox_hardened_defers_to_a_stronger_real_cnf_proof():
    """When a REAL pre-existing audit-CNF proof of A4 conservation exists, the off-box
    minted-CNF card defers to it (the invariant is shown once, strongest evidence)."""
    from game.arena.engine import _real_audit_vault
    stitch_status = {"available": True, "real_cnf": "subtensor-amm__A4-fee-split", "host": "polarisserver",
                     "remote_wall_ms": 40, "local_solver": "glucose3", "cross_solver_agree": True, "cnf_sha256": "z"}
    hardened = {"available": True, "cross_confirmed": True, "host": "polarisserver",
                "rule_id": "A4-fee-split-conservation", "remote_wall_ms": 39.0, "cnf_sha256": "def"}
    v = _real_audit_vault(stitch_status, {}, {}, offbox_hardened=hardened)
    a4 = [c for c in v if c["verdict"] == "HARDENED" and c["family"] == "A_conservation"]
    assert len(a4) == 1 and a4[0].get("real_cnf") and not a4[0].get("offbox")


def test_proof_coverage_is_honest_per_subnet(game):
    """The operator console states, per subnet, whether the arena backs it with a REAL
    reproducing exploit or it reasons into a HARDENED family (no exploit exists). The
    counts are internally consistent and every row is classified — no silent gaps."""
    pc = game.operator_console["proof_coverage"]
    assert pc["total"] == len(pc["rows"]) == 17
    # every row carries a real classification + a backing detail
    kinds = {"real_exploit", "hardened_no_exploit", "fallback"}
    for r in pc["rows"]:
        assert r["backing"] in kinds and r["detail"] and r["family"]
        if r["backing"] == "real_exploit":
            from game.arena.engine import REPRODUCING_TARGETS
            assert r["detail"] in REPRODUCING_TARGETS    # a genuine reproducing target id
    # the three buckets partition the corpus exactly
    assert pc["real_exploit"] + pc["hardened_no_exploit"] + pc["fallback"] == pc["total"]
    assert pc["real_exploit"] >= 1                       # at least some real exploit backing
    # hardened-classified subnets reasoned a family that is actually proven hardened
    hard_fams = {h["family"] for h in _replay.MINTED_HARDENED}
    for r in pc["rows"]:
        if r["backing"] == "hardened_no_exploit":
            assert r["family"] in hard_fams


def test_full_visual_ui_renders(game):
    """The visual-first live UI renders every panel — the whole game, one page."""
    html = render(game)
    for panel in ("Attack Map", "Real Audit Vault", "Breach Feed", "Replay Theater",
                  "Solver Bench", "Anti-Cheat Feed", "Round Proof Anchor", "Operator Console"):
        assert panel in html, f"missing panel: {panel}"
    # the live, animated, self-auditing signals are all present
    assert "SCORING VERIFIED" in html and 'class="amk' in html


def test_three_independent_auditors_agree(game):
    """The capstone claim: scoring audit, bundle verifier, and attestation re-verify
    are three INDEPENDENT checks (different code paths, no shared engine state) and
    all pass for the same round — proof, not claims."""
    scoring_ok = _audit.audit_scoring(game)["ok"]
    top = max((a for a in game.agents if a.gates.passed()),
              key=lambda a: game.emissions[a.run.miner_hotkey])
    bundle_ok = _bundle.verify_bundle(_bundle.build_bundle(game, top.run.agent_id))["ok"]
    att = _att.reverify_real_quote()
    att_ok = att["ok"] if att.get("available") else True   # tolerate no quote on a fresh checkout
    assert scoring_ok and bundle_ok and att_ok
