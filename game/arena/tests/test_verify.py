"""Offline round verifier tests."""
from __future__ import annotations

import json
import shutil
import sys

import pytest

from game.arena import __main__ as arena_main
from game.arena import verify


@pytest.fixture(scope="module")
def generated_round(tmp_path_factory):
    out = tmp_path_factory.mktemp("round")
    old_out = arena_main.OUT
    old_argv = sys.argv[:]
    try:
        arena_main.OUT = out
        sys.argv = ["python", "1"]
        arena_main.main()
    finally:
        arena_main.OUT = old_out
        sys.argv = old_argv
    return out


def copy_round(src, dst):
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return dst


def test_round_verifier_accepts_main_artifacts(generated_round):
    result = verify.verify_round(generated_round)
    assert result["ok"] is True
    required = {c["name"]: c for c in result["checks"] if not c["optional"]}
    assert set(required) == {
        "proof_bundle",
        "signed_vector",
        "weights_consistent",
        "scoring_audit",
        "anchor_consistency",
        "replay_differential",
    }
    assert required["replay_differential"]["status"] == "pass"
    assert "replay harnesses are proven discriminators" in required["replay_differential"]["detail"]
    assert all(c["status"] == "pass" for c in required.values())


def test_round_verifier_rejects_displayed_weights_that_dont_match_the_signature(
        generated_round, tmp_path):
    """The signature only covers the signed vector; a forger could alter the per-agent
    weights the REPORT displays without breaking it. weights_consistent catches that."""
    out = copy_round(generated_round, tmp_path / "forged-display-weights")
    p = out / "score_report.json"
    sr = json.loads(p.read_text(encoding="utf-8"))
    earner = next(a for a in sr["agents"] if a["weight"] > 0)
    earner["weight"] = round(earner["weight"] + 0.25, 4)   # inflate the SHOWN weight only
    p.write_text(json.dumps(sr), encoding="utf-8")
    result = verify.verify_round(out)
    assert result["ok"] is False and "weights_consistent" in result["required_failed"]
    # the signature itself still "verifies" (the signed vector is untouched) — proving
    # this is a DISTINCT check that the display matches the signed truth.
    sig = next(c for c in result["checks"] if c["name"] == "signed_vector")
    assert sig["ok"] is True


def test_round_verifier_rejects_anchor_mismatch(generated_round, tmp_path):
    out = copy_round(generated_round, tmp_path / "bad-anchor")
    anchor_path = out / "round_anchor.json"
    anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
    anchor["merkle_root"] = "bad-root"
    anchor_path.write_text(json.dumps(anchor), encoding="utf-8")

    result = verify.verify_round(out)
    assert result["ok"] is False
    assert "anchor_consistency" in result["required_failed"]


def test_round_verifier_allows_missing_optional_dataset(generated_round, tmp_path):
    out = copy_round(generated_round, tmp_path / "missing-dataset")
    (out / "traces_dataset.json").unlink()

    result = verify.verify_round(out)
    dataset = next(c for c in result["checks"] if c["name"] == "dataset_card")
    assert result["ok"] is True
    assert dataset["optional"] is True
    assert dataset["status"] == "absent"


# -- tamper detection: a verifier that only ever says OK proves nothing -----------

def test_round_verifier_rejects_a_forged_signed_vector(generated_round, tmp_path):
    out = copy_round(generated_round, tmp_path / "forged-vector")
    p = out / "score_report.json"
    sr = json.loads(p.read_text(encoding="utf-8"))
    sr["signed_vector"]["weights"][0]["weight"] = 0.999999     # flip a weight, do NOT re-sign
    p.write_text(json.dumps(sr), encoding="utf-8")
    result = verify.verify_round(out)
    assert result["ok"] is False and "signed_vector" in result["required_failed"]


def test_round_verifier_rejects_a_broken_proof_bundle(generated_round, tmp_path):
    out = copy_round(generated_round, tmp_path / "broken-bundle")
    p = out / "proof_bundle.json"
    pb = json.loads(p.read_text(encoding="utf-8"))
    pb["nonce"] = "tampered-nonce"                     # breaks the signed receipt binding
    p.write_text(json.dumps(pb), encoding="utf-8")
    result = verify.verify_round(out)
    assert result["ok"] is False and "proof_bundle" in result["required_failed"]


def test_round_verifier_rejects_a_forged_offbox_receipt(generated_round, tmp_path):
    out = copy_round(generated_round, tmp_path / "forged-offbox")
    # claims success but cnf_satisfied is False -> the optional check must FAIL (not pass)
    (out / "offbox_stitch_receipt.json").write_text(json.dumps({
        "available": True, "ok": True, "cnf_satisfied": False,
        "host": "polarisserver", "solver": "kissat"}), encoding="utf-8")
    result = verify.verify_round(out)
    offbox = next(c for c in result["checks"] if c["name"] == "offbox_receipt")
    assert offbox["status"] == "fail" and offbox["ok"] is False


def test_round_verifier_handles_a_missing_dir_without_crashing(tmp_path):
    result = verify.verify_round(tmp_path / "nope")
    assert result["ok"] is False and "proof_bundle" in result["required_failed"]


def test_verify_cli_json_outputs_machine_readable_result(generated_round, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["python", str(generated_round), "--json"])
    assert verify.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["dir"] == str(generated_round)
    assert any(c["name"] == "signed_vector" for c in payload["checks"])


def test_verifier_checks_the_i1_offbox_receipt(generated_round, tmp_path):
    """The multi-rule off-box capture (I1) is an optional check: a genuine I1 receipt
    (cnf_satisfied, rule_id=I1) passes; a non-I1 or unsatisfied one fails."""
    good = copy_round(generated_round, tmp_path / "i1-ok")
    (good / "offbox_i1_receipt.json").write_text(json.dumps({
        "available": True, "ok": True, "cnf_satisfied": True, "rule_id": "I1-div-by-zero",
        "host": "polarisserver", "solver": "kissat", "n_lits": 4032}), encoding="utf-8")
    c = next(x for x in verify.verify_round(good)["checks"] if x["name"] == "offbox_i1")
    assert c["status"] == "pass"

    bad = copy_round(generated_round, tmp_path / "i1-bad")
    (bad / "offbox_i1_receipt.json").write_text(json.dumps({
        "available": True, "ok": True, "cnf_satisfied": False,   # not actually satisfied
        "rule_id": "I1-div-by-zero", "host": "polarisserver", "solver": "kissat"}), encoding="utf-8")
    c2 = next(x for x in verify.verify_round(bad)["checks"] if x["name"] == "offbox_i1")
    assert c2["status"] == "fail" and c2["ok"] is False


def test_verifier_accepts_and_rejects_a_hardened_offbox_receipt(generated_round, tmp_path):
    """The off-box HARDENED receipt (defensive: cross-confirmed UNSAT) is an optional
    check — a genuine one passes; one that claims success without cross-confirmation fails."""
    good = copy_round(generated_round, tmp_path / "hardened-ok")
    (good / "offbox_hardened_receipt.json").write_text(json.dumps({
        "available": True, "host": "polarisserver", "solver": "kissat",
        "rule_id": "A4-fee-split-conservation", "remote_unsat": True,
        "local_unsat": True, "cross_confirmed": True}), encoding="utf-8")
    c = next(x for x in verify.verify_round(good)["checks"] if x["name"] == "offbox_hardened")
    assert c["status"] == "pass"

    bad = copy_round(generated_round, tmp_path / "hardened-bad")
    (bad / "offbox_hardened_receipt.json").write_text(json.dumps({
        "available": True, "remote_unsat": True, "local_unsat": False,   # local did NOT agree
        "cross_confirmed": False, "host": "polarisserver", "solver": "kissat"}), encoding="utf-8")
    c2 = next(x for x in verify.verify_round(bad)["checks"] if x["name"] == "offbox_hardened")
    assert c2["status"] == "fail" and c2["ok"] is False


def test_every_winner_ships_a_verifiable_bundle(generated_round):
    """Each breaching miner — not just the top earner — exports an independently
    re-checkable proof bundle, and they all verify."""
    payload = json.loads((generated_round / "proof_bundles.json").read_text(encoding="utf-8"))
    assert payload["count"] >= 2 and payload["count"] == len(payload["bundles"])
    from game.arena.bundle import verify_bundle
    assert all(verify_bundle(b)["ok"] for b in payload["bundles"])   # every winner verifies
    check = next(c for c in verify.verify_round(generated_round)["checks"]
                 if c["name"] == "all_winner_bundles")
    assert check["status"] == "pass"


def test_a_tampered_non_top_winner_bundle_is_caught(generated_round, tmp_path):
    out = copy_round(generated_round, tmp_path / "tampered-winner")
    p = out / "proof_bundles.json"
    payload = json.loads(p.read_text(encoding="utf-8"))
    payload["bundles"][-1]["nonce"] = "tampered"        # break a NON-top winner's binding
    p.write_text(json.dumps(payload), encoding="utf-8")
    result = verify.verify_round(out)
    check = next(c for c in result["checks"] if c["name"] == "all_winner_bundles")
    assert check["status"] == "fail" and check["ok"] is False


# -- season ledger integrity (cumulative cross-round) -----------------------------

import copy  # noqa: E402

from game.arena.engine import ArenaEngine  # noqa: E402


def _season_state(tmp_path):
    p = tmp_path / "season_state.json"
    ArenaEngine().run_season(2, state_path=str(p))
    return json.loads(p.read_text(encoding="utf-8"))


def test_verify_season_accepts_a_real_ledger(tmp_path):
    state = _season_state(tmp_path)
    res = verify.verify_season(state)
    assert res["ok"] is True and res["rounds"] == 2 and res["n_agents"] > 0 and not res["violations"]


def test_verify_season_rejects_impossible_counters(tmp_path):
    state = _season_state(tmp_path)
    hk = next(iter(state["agents"]))

    neg = copy.deepcopy(state); neg["agents"][hk]["total_emissions"] = -5
    assert verify.verify_season(neg)["ok"] is False

    over = copy.deepcopy(state); over["agents"][hk]["rounds_played"] = 99
    assert verify.verify_season(over)["ok"] is False           # rounds_played > season rounds

    free = copy.deepcopy(state)
    for a in free["agents"].values():
        a["breaches"] = 0                                       # earnings without breaches
    earners = [a for a in free["agents"].values() if a["total_emissions"] > 0]
    assert earners and verify.verify_season(free)["ok"] is False


def test_round_verifier_surfaces_season_ledger_when_present(tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(arena_main, "OUT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["python", "--season", "2"])
    arena_main.main()                                           # writes season_state.json
    res = verify.verify_round(tmp_path)
    season = next(c for c in res["checks"] if c["name"] == "season_ledger")
    assert season["optional"] is True and season["status"] == "pass"
    assert res["ok"] is True


def test_e2e_self_verifies_and_persists_a_verdict(generated_round):
    """The E2E run re-checks its OWN artifacts offline and writes round_verdict.json —
    so every round ships a self-contained, passing, independent verdict."""
    verdict = json.loads((generated_round / "round_verdict.json").read_text(encoding="utf-8"))
    assert verdict["ok"] is True and not verdict["required_failed"]
    # the persisted verdict matches a fresh re-verification of the same dir
    assert verify.verify_round(generated_round)["ok"] is True
