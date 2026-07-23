"""Unit tests for the validator release gate's parsing + threshold logic.

These exercise the pure evaluate_* functions with mocked inputs — no network,
no chain. Network I/O (fetch_feed / fetch_chain) is intentionally not tested
here; it has no decision logic.
"""

from __future__ import annotations

import importlib.util
import math
import os
from datetime import datetime, timedelta, timezone

import pytest

from scaffold.publisher import health_thresholds as ht

# Load the script module by path (scripts/ is not an importable package).
_GATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "scripts",
    "validator_release_gate.py",
)
_spec = importlib.util.spec_from_file_location("validator_release_gate", _GATE_PATH)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---- timestamp parsing ----------------------------------------------------
def test_parse_iso_handles_z_suffix():
    assert gate.parse_iso("2026-06-27T12:00:00.000Z") is not None
    assert gate.parse_iso("2026-06-27T12:00:00+00:00") is not None


def test_parse_iso_bad_input_returns_none():
    assert gate.parse_iso(None) is None
    assert gate.parse_iso("") is None
    assert gate.parse_iso("not-a-timestamp") is None


def test_age_seconds_uses_now_reference():
    now = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)
    gen = now - timedelta(seconds=90)
    age = gate.age_seconds(_iso(gen), now=now.timestamp())
    assert abs(age - 90.0) < 0.01


def test_age_seconds_none_for_unparseable():
    assert gate.age_seconds("garbage") is None


# ---- feed 5xx check -------------------------------------------------------
def test_feed_status_200_passes():
    assert gate.evaluate_feed_status(200, None)["passed"] is True


def test_feed_status_4xx_passes_not_a_5xx():
    # A 4xx is not a server fault for the gate's "no 5xx" check.
    assert gate.evaluate_feed_status(404, None)["passed"] is True


def test_feed_status_5xx_fails():
    assert gate.evaluate_feed_status(503, None)["passed"] is False
    assert gate.evaluate_feed_status(500, None)["passed"] is False


def test_feed_unreachable_fails():
    res = gate.evaluate_feed_status(None, "ConnectionRefused")
    assert res["passed"] is False
    assert "unreachable" in res["detail"]


# ---- signed-vector age check ----------------------------------------------
def test_vector_age_within_limit_passes():
    assert gate.evaluate_vector_age(120.0)["passed"] is True
    assert gate.evaluate_vector_age(ht.GATE_VECTOR_MAX_AGE_SECONDS)["passed"] is True


def test_vector_age_over_limit_fails():
    assert gate.evaluate_vector_age(ht.GATE_VECTOR_MAX_AGE_SECONDS + 1)["passed"] is False


def test_vector_age_missing_fails():
    assert gate.evaluate_vector_age(None)["passed"] is False


# ---- Cathedral validator update age check --------------------------------
def test_uid_update_age_fresh_passes():
    # 30 blocks * 12s = 360s, well within one configured cycle.
    res = gate.evaluate_uid_update_age(30)
    assert res["passed"] is True


def test_uid_update_age_at_limit_passes():
    # 1500s cadence + 120s scheduling grace = 135 blocks.
    res = gate.evaluate_uid_update_age(135)
    assert res["passed"] is True


def test_uid_update_age_stale_fails():
    # One block beyond the configured cycle plus bounded grace fails.
    res = gate.evaluate_uid_update_age(136)
    assert res["passed"] is False


def test_uid_update_age_respects_longer_chain_rate_limit():
    # A 100-block chain rate limit plus 120s grace permits 110 blocks,
    # even when a caller configures an impossible 600s loop interval.
    assert gate.evaluate_uid_update_age(
        110,
        validator_interval_seconds=600,
        weights_rate_limit_blocks=100,
    )["passed"] is True
    assert gate.evaluate_uid_update_age(
        111,
        validator_interval_seconds=600,
        weights_rate_limit_blocks=100,
    )["passed"] is False


def test_uid_update_age_rejects_invalid_cadence_configuration():
    res = gate.evaluate_uid_update_age(1, validator_interval_seconds=0)
    assert res["passed"] is False
    assert "invalid" in res["detail"]
    assert gate.evaluate_uid_update_age(
        1, validator_interval_seconds=math.nan
    )["passed"] is False


def test_cli_rejects_nonfinite_validator_interval():
    with pytest.raises(SystemExit) as exc:
        gate.main(["--validator-interval-seconds", "nan"])
    assert exc.value.code == 2


def test_uid_update_age_no_chain_fails():
    assert gate.evaluate_uid_update_age(None)["passed"] is False


# ---- validators-fresh-within-tempo check ----------------------------------
def test_fresh_validators_counts_only_permitted_and_fresh():
    blocks_since = {1: 10, 2: 400, 3: 50, 4: 5}
    permits = {1: True, 2: True, 3: False, 4: True}
    # permitted = {1,2,4}; fresh (<=360 blocks) among permitted = {1,4}
    res = gate.evaluate_fresh_validators(blocks_since, permits, min_fresh=2)
    assert res["fresh_uids"] == [1, 4]
    assert res["permitted_count"] == 3
    assert res["passed"] is True


def test_fresh_validators_below_quorum_fails():
    blocks_since = {1: 10, 2: 400}
    permits = {1: True, 2: True}
    res = gate.evaluate_fresh_validators(blocks_since, permits, min_fresh=2)
    # only uid1 fresh -> need 2 -> fail
    assert res["passed"] is False


def test_fresh_validators_tempo_boundary_is_inclusive():
    blocks_since = {1: ht.TEMPO_BLOCKS}  # exactly 360 blocks counts as fresh
    permits = {1: True}
    res = gate.evaluate_fresh_validators(blocks_since, permits, min_fresh=1)
    assert res["passed"] is True


def test_fresh_validators_no_chain_fails():
    assert gate.evaluate_fresh_validators(None, None)["passed"] is False
    assert gate.evaluate_fresh_validators({}, {})["passed"] is False


# ---- gate aggregation -----------------------------------------------------
def test_gate_passed_all_true():
    checks = [{"passed": True}, {"passed": True}]
    assert gate.gate_passed(checks) is True


def test_gate_passed_any_false():
    checks = [{"passed": True}, {"passed": False}]
    assert gate.gate_passed(checks) is False


# ---- thresholds module ----------------------------------------------------
def test_tempo_constant_is_72_minutes():
    assert ht.TEMPO_BLOCKS == 360
    assert ht.TEMPO_SECONDS == 4320  # 72 min


def test_vector_status_levels():
    assert ht.vector_status(60)["level"] == "ok"        # <= 2 min
    assert ht.vector_status(360)["level"] == "warn"     # > 5 min
    assert ht.vector_status(700)["level"] == "page"     # > 10 min
    assert ht.vector_status(None)["level"] == "unknown"


def test_vector_status_hard_ceiling_flag():
    assert ht.vector_status(5000)["over_hard_ceiling"] is True   # > 72 min
    assert ht.vector_status(60)["over_hard_ceiling"] is False


def test_uid200_status_levels():
    assert ht.uid200_status(120)["level"] == "ok"       # <= 5 min
    assert ht.uid200_status(700)["level"] == "warn"     # > 10 min
    assert ht.uid200_status(1300)["level"] == "page"    # > 20 min
    assert ht.uid200_status(None)["level"] == "unknown"


# ---- all-three-URL compatibility ------------------------------------------
def test_compat_urls_covers_all_three():
    urls = gate.compat_urls("https://api.cathedral.computer", "https://read.cathedral.computer")
    labels = {label for label, _ in urls}
    assert labels == {"canonical", "legacy_prefixed", "read_service"}
    by = dict(urls)
    assert by["canonical"] == "https://api.cathedral.computer/v1/validator/weights/next"
    assert by["legacy_prefixed"] == (
        "https://api.cathedral.computer/api/cathedral/v1/validator/weights/next")
    assert by["read_service"] == "https://read.cathedral.computer/v1/validator/weights/next"


def _fresh_feed(sig: str = "AAAA"):
    now = datetime.now(timezone.utc)
    return {"status": 200, "error": None,
            "body": {"signature": sig, "generated_at": _iso(now)}}


def test_url_compat_fresh_200_passes_and_exposes_signature():
    res = gate.evaluate_url_compat("canonical", _fresh_feed("SIGX"))
    assert res["passed"] is True
    assert res["signature"] == "SIGX"


def test_url_compat_5xx_fails():
    res = gate.evaluate_url_compat("read_service", {"status": 503, "error": None, "body": None})
    assert res["passed"] is False
    assert "5xx" in res["detail"]


def test_url_compat_unreachable_fails():
    res = gate.evaluate_url_compat("legacy_prefixed",
                                   {"status": None, "error": "ConnRefused", "body": None})
    assert res["passed"] is False


def test_url_compat_200_without_signature_fails():
    res = gate.evaluate_url_compat("canonical",
                                   {"status": 200, "error": None, "body": {"generated_at": "x"}})
    assert res["passed"] is False


def test_url_compat_stale_vector_fails():
    old = datetime.now(timezone.utc) - timedelta(seconds=ht.GATE_VECTOR_MAX_AGE_SECONDS + 60)
    res = gate.evaluate_url_compat(
        "canonical", {"status": 200, "error": None,
                      "body": {"signature": "S", "generated_at": _iso(old)}})
    assert res["passed"] is False


def test_same_signed_bytes_match_passes():
    res = gate.evaluate_same_signed_bytes(
        {"canonical": "S", "legacy_prefixed": "S", "read_service": "S"})
    assert res["passed"] is True


def test_same_signed_bytes_divergence_fails():
    res = gate.evaluate_same_signed_bytes(
        {"canonical": "S", "legacy_prefixed": "DIFFERENT", "read_service": "S"})
    assert res["passed"] is False
    assert "DIVERGED" in res["detail"]


def test_same_signed_bytes_one_url_passes_vacuously():
    # an unreachable URL contributes no signature; <2 sigs = nothing to compare.
    res = gate.evaluate_same_signed_bytes({"canonical": "S", "read_service": None})
    assert res["passed"] is True


# ---- manual checks + gate aggregation -------------------------------------
def test_manual_check_is_not_auto_failed():
    m = gate.manual_check("burn_snapshot_matches_policy", "confirm by hand")
    assert m["manual"] is True
    assert m["passed"] is None
    # a manual (passed=None) item does not flip the gate to FAIL.
    assert gate.gate_passed([{"passed": True}, m]) is True
    # but a concrete failure still fails the gate even alongside manual items.
    assert gate.gate_passed([{"passed": False}, m]) is False


# ---- bittensor version pin ------------------------------------------------
def test_bittensor_major_matches_pin():
    assert gate.check_bittensor_major("10.4.1") is None
    assert gate.check_bittensor_major(f"{gate.BITTENSOR_REQUIRED_MAJOR}.0.0") is None


def test_bittensor_major_mismatch_reports_reason():
    reason = gate.check_bittensor_major("9.2.0")
    assert reason is not None and "!= pinned" in reason


def test_bittensor_version_missing_or_garbage():
    assert gate.check_bittensor_major(None) is not None
    assert gate.check_bittensor_major("not-a-version") is not None
