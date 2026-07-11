"""Unit tests for the SAT fast-path score poster (Release-1 "10% reward wire").

These exercise the pure normalize/build functions with a sample v2 scoreboard
and confirm the resulting report is accepted by the real
``external_scores.normalize_report`` — i.e. the poster produces exactly what
the hardened intake expects, with no network involved. HTTP (fetch/post) is
mocked via monkeypatch; nothing here hits the network.
"""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timezone

import pytest

from scaffold.publisher import external_scores

# Load the script module by path (scripts/ is not an importable package),
# same pattern as test_validator_release_gate.py.
_POSTER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "scripts",
    "sat_fast_score_poster.py",
)
_spec = importlib.util.spec_from_file_location("sat_fast_score_poster", _POSTER_PATH)
poster = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poster)


NOW = datetime(2026, 7, 4, 13, 0, 0, tzinfo=timezone.utc)

SAMPLE_SCOREBOARD = {
    "schema": "cathedral.v2.shadow_weights.v1",
    "vector_id": "e7bcddec-85ab-4a76-a34f-df81d73b8fb6",
    "policy_version": 1783171419299,
    "generated_at": "2026-07-04T13:23:39.263Z",
    "expires_at": "2026-07-04T13:53:39.263Z",
    "policy_hash": "sha256:841282d5797097271fb0c2f57468db83c8ebbfceee16e65209e59abdb794274a",
    "key_id": "cathedral-weight-policy",
    "policy_reason": "v2_shadow_verified_receipts_24h",
    "policy_metadata": {"shadow": True},
    "weights": [
        {"miner_hotkey": "5D7XHj7p8q1mbByu4iN5HraJGBHraxC9aRkuDKZW6NQp24p4",
         "weight": 1.0, "raw_score": 24.0},
        {"miner_hotkey": "5FakeSecond0000000000000000000000000000000000",
         "weight": 0.5, "raw_score": 12.0},
    ],
    "signature": "deadbeef==",
}


# ---- normalize_scores -------------------------------------------------------
def test_normalize_scores_extracts_hotkey_and_raw_score():
    out = poster.normalize_scores(SAMPLE_SCOREBOARD["weights"])
    assert out == [
        ("5D7XHj7p8q1mbByu4iN5HraJGBHraxC9aRkuDKZW6NQp24p4", 24.0),
        ("5FakeSecond0000000000000000000000000000000000", 12.0),
    ]


def test_normalize_scores_falls_back_to_weight_when_raw_score_missing():
    out = poster.normalize_scores([{"miner_hotkey": "5X", "weight": 0.75}])
    assert out == [("5X", 0.75)]


def test_normalize_scores_skips_empty_hotkey_and_nonpositive_values():
    out = poster.normalize_scores([
        {"miner_hotkey": "", "raw_score": 5.0},
        {"miner_hotkey": "5Zero", "raw_score": 0.0},
        {"miner_hotkey": "5Neg", "raw_score": -1.0},
        {"miner_hotkey": "5NaN", "raw_score": float("nan")},
        {"miner_hotkey": None, "raw_score": 5.0},
    ])
    assert out == []


def test_normalize_scores_dedupes_by_hotkey():
    out = poster.normalize_scores([
        {"miner_hotkey": "5Dup", "raw_score": 1.0},
        {"miner_hotkey": "5Dup", "raw_score": 99.0},
    ])
    assert out == [("5Dup", 1.0)]


# ---- build_report -----------------------------------------------------------
def test_build_report_normalizes_to_0_1_by_max():
    report = poster.build_report(SAMPLE_SCOREBOARD, now=NOW)
    assert report is not None
    assert report["source"] == "cathedral_sat_fast"
    assert report["mechanism"] == "cathedral_sat_fast"
    scores = {s["miner_hotkey"]: s["score"] for s in report["scores"]}
    assert scores["5D7XHj7p8q1mbByu4iN5HraJGBHraxC9aRkuDKZW6NQp24p4"] == pytest.approx(1.0)
    assert scores["5FakeSecond0000000000000000000000000000000000"] == pytest.approx(0.5)
    for s in report["scores"]:
        assert 0.0 <= s["score"] <= 1.0


def test_build_report_carries_upstream_metadata():
    report = poster.build_report(SAMPLE_SCOREBOARD, now=NOW)
    assert report["metadata"]["upstream_vector_id"] == SAMPLE_SCOREBOARD["vector_id"]
    assert report["metadata"]["upstream_policy_version"] == SAMPLE_SCOREBOARD["policy_version"]


def test_build_report_empty_weights_returns_none():
    assert poster.build_report({"weights": []}, now=NOW) is None
    assert poster.build_report({}, now=NOW) is None
    assert poster.build_report({"weights": None}, now=NOW) is None


def test_build_report_all_zero_scores_returns_none():
    degraded = {"weights": [
        {"miner_hotkey": "5A", "raw_score": 0.0, "weight": 0.0},
        {"miner_hotkey": "5B", "raw_score": -1.0},
    ]}
    assert poster.build_report(degraded, now=NOW) is None


def test_build_report_not_a_dict_returns_none():
    assert poster.build_report(None, now=NOW) is None
    assert poster.build_report([], now=NOW) is None


# ---- interop with the real hardened intake ----------------------------------
def test_report_accepted_by_normalize_report():
    """The poster's report must be exactly what the real intake accepts —
    this is the whole point of reusing the hardened blend rather than
    building a new one."""
    report = poster.build_report(SAMPLE_SCOREBOARD, now=NOW)
    normalized = external_scores.normalize_report(report, default_source="violet_audio", now=NOW)
    assert normalized["source"] == "cathedral_sat_fast"
    assert normalized["source"] in external_scores.ALLOWED_ENDPOINT_SOURCES
    assert len(normalized["scores"]) == len(report["scores"])
    for s in normalized["scores"]:
        assert 0.0 <= s["score"] <= 1.0


def test_report_json_roundtrip_is_canonical():
    report = poster.build_report(SAMPLE_SCOREBOARD, now=NOW)
    body = poster.canonical_body(report)
    assert json.loads(body.decode("utf-8")) == report


# ---- HMAC scheme reuse -------------------------------------------------------
def test_hmac_header_matches_verify_hmac_scheme(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET", "s3cr3t")
    report = poster.build_report(SAMPLE_SCOREBOARD, now=NOW)
    body = poster.canonical_body(report)
    header = poster.compute_hmac_header(body, "s3cr3t")
    assert header.startswith("sha256=")
    assert external_scores.verify_hmac(body, header) is True
    assert external_scores.verify_hmac(body, "sha256=wrong") is False


# ---- run_once: dry-run / empty scoreboard / mocked POST ---------------------
class _Args:
    def __init__(self, **kw):
        self.challenge_base = "https://example.invalid/scoreboard"
        self.submit_base = "https://example.invalid"
        self.source = "cathedral_sat_fast"
        self.dry_run = False
        self.timeout = 5.0
        self.token = "tok"
        self.hmac_secret = None
        self.__dict__.update(kw)


def test_run_once_dry_run_never_posts(monkeypatch, capsys):
    monkeypatch.setattr(poster, "fetch_scoreboard",
                         lambda url, timeout=10.0: {"status": 200, "body": SAMPLE_SCOREBOARD, "error": None})

    def _boom(*a, **kw):
        raise AssertionError("post_report must not be called in --dry-run")

    monkeypatch.setattr(poster, "post_report", _boom)
    args = _Args(dry_run=True)
    rc = poster.run_once(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "not posting" in out


def test_run_once_empty_scoreboard_posts_nothing_exits_0(monkeypatch):
    monkeypatch.setattr(poster, "fetch_scoreboard",
                         lambda url, timeout=10.0: {"status": 200, "body": {"weights": []}, "error": None})

    def _boom(*a, **kw):
        raise AssertionError("post_report must not be called for an empty scoreboard")

    monkeypatch.setattr(poster, "post_report", _boom)
    args = _Args()
    rc = poster.run_once(args)
    assert rc == 0


def test_run_once_fetch_error_exits_nonzero(monkeypatch):
    monkeypatch.setattr(poster, "fetch_scoreboard",
                         lambda url, timeout=10.0: {"status": None, "body": None, "error": "boom"})
    args = _Args()
    rc = poster.run_once(args)
    assert rc != 0


def test_run_once_posts_normalized_report_on_success(monkeypatch):
    monkeypatch.setattr(poster, "fetch_scoreboard",
                         lambda url, timeout=10.0: {"status": 200, "body": SAMPLE_SCOREBOARD, "error": None})

    posted = {}

    def _fake_post(url, report, *, token, hmac_secret, timeout=10.0):
        posted["url"] = url
        posted["report"] = report
        posted["token"] = token
        posted["hmac_secret"] = hmac_secret
        return {"status": 202, "body": {"status": "accepted"}, "error": None}

    monkeypatch.setattr(poster, "post_report", _fake_post)
    args = _Args(token="secret-token")
    rc = poster.run_once(args)
    assert rc == 0
    assert posted["url"] == "https://example.invalid/v1/external-scores/violet"
    assert posted["report"]["source"] == "cathedral_sat_fast"
    assert posted["token"] == "secret-token"
    # the report itself must validate against the real hardened intake
    # use current time since run_once uses datetime.now() to set generated_at
    external_scores.normalize_report(posted["report"], default_source="violet_audio")


def test_run_once_post_failure_exits_nonzero(monkeypatch):
    monkeypatch.setattr(poster, "fetch_scoreboard",
                         lambda url, timeout=10.0: {"status": 200, "body": SAMPLE_SCOREBOARD, "error": None})
    monkeypatch.setattr(poster, "post_report",
                         lambda *a, **kw: {"status": None, "body": None, "error": "connection refused"})
    args = _Args()
    rc = poster.run_once(args)
    assert rc != 0


def test_run_once_post_rejected_status_exits_nonzero(monkeypatch):
    monkeypatch.setattr(poster, "fetch_scoreboard",
                         lambda url, timeout=10.0: {"status": 200, "body": SAMPLE_SCOREBOARD, "error": None})
    monkeypatch.setattr(poster, "post_report",
                         lambda *a, **kw: {"status": 401, "body": {"detail": "invalid_external_scores_token"}, "error": None})
    args = _Args()
    rc = poster.run_once(args)
    assert rc != 0
