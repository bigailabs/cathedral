"""Tests for scaffold/publisher/cybergym_contract.py.

The shared document contract: one canonicalization, one key list, one HMAC
verification, used by both the intake route and the composition adapter. These
tests pin the properties the two sides depend on, and the read-side verification
that makes ``sig_ok`` mean something.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from scaffold.publisher import cybergym_contract as contract

SECRET = "contract-test-secret"


def _document(**overrides) -> dict:
    doc = {
        "producer_hotkey": "5Producer",
        "network": "test",
        "netuid": 1234,
        "source_epoch": 4,
        "generated_at": "2026-07-29T00:00:00.000Z",
        "complete": True,
        "score_units": "level_weighted_verified_solves",
        "scores": {"5Alice": 3.0, "5Bob": 1.0},
        "evidence_sha256": "d" * 64,
    }
    doc.update(overrides)
    return doc


def _stored(monkeypatch, doc: dict, **column_overrides):
    monkeypatch.setenv(contract.HMAC_SECRET_ENV, SECRET)
    body = contract.canonical_report_bytes(contract.semantic_view(doc))
    header = {
        "network": doc["network"],
        "netuid": doc["netuid"],
        "source_epoch": doc["source_epoch"],
        "producer_hotkey": doc["producer_hotkey"],
        "complete": 1 if doc["complete"] else 0,
        "generated_at_iso": doc["generated_at"],
        "report_sha256": contract.report_digest(doc),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "score_count": len(doc["scores"]),
    }
    header.update(column_overrides)
    signature = "sha256=" + contract.body_hmac_hex(body, SECRET)
    return header, body, signature


def test_canonicalization_is_stable_and_key_order_independent():
    a = contract.canonical_report_bytes({"b": 1, "a": 2})
    b = contract.canonical_report_bytes({"a": 2, "b": 1})
    assert a == b == b'{"a":2,"b":1}'


def test_semantic_view_drops_derived_fields():
    doc = _document()
    doc["report_sha256"] = "x" * 64
    doc["report_id"] = "cyg-whatever"
    doc["authenticated_body"] = "{}"
    view = contract.semantic_view(doc)
    assert set(view) == set(contract.SEMANTIC_KEYS)
    # A derived field can therefore never change the identity of a report.
    assert contract.report_digest(doc) == contract.report_digest(view)


def test_receipt_id_is_deterministic_and_namespaced():
    digest = contract.report_digest(_document())
    assert contract.receipt_id(digest) == contract.receipt_id(digest)
    assert contract.receipt_id(digest).startswith("cyg-")
    assert contract.receipt_id(digest) != contract.receipt_id("0" * 64)


def test_constant_time_equal_tolerates_non_ascii():
    assert contract.constant_time_equal("abc", "abc") is True
    assert contract.constant_time_equal("abcé", "abc") is False


def test_verify_accepts_a_genuine_report(monkeypatch):
    doc = _document()
    header, body, signature = _stored(monkeypatch, doc)
    out = contract.verify_stored_report(
        header, body=body, signature=signature, rows=dict(doc["scores"])
    )
    assert out["scores"] == {"5Alice": 3.0, "5Bob": 1.0}
    assert out["document"]["source_epoch"] == 4


def test_verify_accepts_bytes_or_text(monkeypatch):
    doc = _document()
    header, body, signature = _stored(monkeypatch, doc)
    for form in (body, body.decode("utf-8")):
        assert contract.verify_stored_report(
            header, body=form, signature=signature
        )["scores"]


def test_authenticated_wire_body_is_bound_to_the_normalized_semantics(monkeypatch):
    """The split raw/canonical representation must not create a second truth."""
    monkeypatch.setenv(contract.HMAC_SECRET_ENV, SECRET)
    raw_document = _document(
        generated_at="2026-07-29T00:00:00+00:00",
        scores={"5Alice": 3, "5Bob": 1},
    )
    raw_body = contract.canonical_report_bytes(raw_document)
    normalized = contract.normalize_semantic_document(raw_document)
    semantic_body = contract.canonical_report_bytes(normalized)
    header = {
        "network": normalized["network"],
        "netuid": normalized["netuid"],
        "source_epoch": normalized["source_epoch"],
        "producer_hotkey": normalized["producer_hotkey"],
        "complete": 1,
        "generated_at_iso": normalized["generated_at"],
        "report_sha256": contract.report_digest(normalized),
        "body_sha256": hashlib.sha256(raw_body).hexdigest(),
        "score_count": len(normalized["scores"]),
    }
    signature = "sha256=" + contract.body_hmac_hex(raw_body, SECRET)

    verified = contract.verify_stored_report(
        header,
        body=semantic_body,
        authenticated_body=raw_body,
        signature=signature,
        rows=normalized["scores"],
    )
    assert verified["document"] == normalized

    forged = dict(normalized)
    forged["scores"] = {"5Attacker": 999.0}
    with pytest.raises(contract.ReportVerificationError) as exc:
        contract.verify_stored_report(
            header,
            body=contract.canonical_report_bytes(forged),
            authenticated_body=raw_body,
            signature=signature,
            rows=normalized["scores"],
        )
    assert exc.value.reason == "authenticated_semantics_mismatch"


def test_pre_split_noncanonical_body_normalizes_after_hmac_verification(monkeypatch):
    """Migration 0049 must not burn a valid row written by migration 0048."""
    monkeypatch.setenv(contract.HMAC_SECRET_ENV, SECRET)
    legacy_document = _document(
        generated_at="2026-07-29T00:00:00+00:00",
        scores={"5Alice": 3, "5Bob": 1},
    )
    legacy_body = json.dumps(
        legacy_document, indent=2, sort_keys=False
    ).encode("utf-8")
    normalized = contract.normalize_semantic_document(legacy_document)
    header = {
        "network": normalized["network"],
        "netuid": normalized["netuid"],
        "source_epoch": normalized["source_epoch"],
        "producer_hotkey": normalized["producer_hotkey"],
        "complete": 1,
        "generated_at_iso": normalized["generated_at"],
        "report_sha256": contract.report_digest(normalized),
        "body_sha256": hashlib.sha256(legacy_body).hexdigest(),
        "score_count": len(normalized["scores"]),
    }
    signature = "sha256=" + contract.body_hmac_hex(legacy_body, SECRET)

    verified = contract.verify_stored_report(
        header,
        body=legacy_body,
        authenticated_body="",
        signature=signature,
        rows=normalized["scores"],
    )
    assert verified["document"] == normalized


@pytest.mark.parametrize("reason,mutate", [
    ("body_missing", lambda h, b, s: (h, None, s)),
    ("body_digest_mismatch", lambda h, b, s: ({**h, "body_sha256": "0" * 64}, b, s)),
    ("body_digest_mismatch", lambda h, b, s: ({**h, "body_sha256": ""}, b, s)),
    ("signature_invalid", lambda h, b, s: (h, b, "sha256=" + "f" * 64)),
    ("signature_invalid", lambda h, b, s: (h, b, "")),
    ("report_digest_mismatch", lambda h, b, s: ({**h, "report_sha256": "1" * 64}, b, s)),
    ("header_mismatch", lambda h, b, s: ({**h, "source_epoch": 99}, b, s)),
    ("header_mismatch", lambda h, b, s: ({**h, "netuid": 7}, b, s)),
    ("header_mismatch", lambda h, b, s: ({**h, "network": "finney"}, b, s)),
    ("header_mismatch", lambda h, b, s: ({**h, "producer_hotkey": "5Other"}, b, s)),
    ("header_mismatch", lambda h, b, s: ({**h, "generated_at_iso": "2020-01-01T00:00:00.000Z"}, b, s)),
    ("header_mismatch", lambda h, b, s: ({**h, "complete": 0}, b, s)),
    ("header_mismatch", lambda h, b, s: ({**h, "score_count": 9}, b, s)),
])
def test_verify_rejects_each_corruption(monkeypatch, reason, mutate):
    header, body, signature = _stored(monkeypatch, _document())
    header, body, signature = mutate(header, body, signature)
    with pytest.raises(contract.ReportVerificationError) as exc:
        contract.verify_stored_report(header, body=body, signature=signature)
    assert exc.value.reason == reason


def test_verify_requires_a_configured_secret(monkeypatch):
    header, body, signature = _stored(monkeypatch, _document())
    monkeypatch.delenv(contract.HMAC_SECRET_ENV, raising=False)
    with pytest.raises(contract.ReportVerificationError) as exc:
        contract.verify_stored_report(header, body=body, signature=signature)
    assert exc.value.reason == "signature_unverifiable"


@pytest.mark.parametrize("body_text,detail", [
    ("not json", "body is not JSON"),
    ("[1,2,3]", "body is not an object"),
    ('{"producer_hotkey":"5P"}', "missing"),
])
def test_verify_rejects_a_malformed_body(monkeypatch, body_text, detail):
    monkeypatch.setenv(contract.HMAC_SECRET_ENV, SECRET)
    body = body_text.encode("utf-8")
    header = {
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "report_sha256": "0" * 64,
    }
    signature = "sha256=" + contract.body_hmac_hex(body, SECRET)
    with pytest.raises(contract.ReportVerificationError) as exc:
        contract.verify_stored_report(header, body=body, signature=signature)
    assert exc.value.reason == "malformed_report"
    assert detail in exc.value.detail


@pytest.mark.parametrize("rows", [
    {"5Alice": 3.0},                                  # a row was deleted
    {"5Alice": 3.0, "5Bob": 1.0, "5Ghost": 2.0},      # a row was added
    {"5Alice": 999999.0, "5Bob": 1.0},                # a row was inflated
])
def test_verify_detects_row_tampering(monkeypatch, rows):
    doc = _document()
    header, body, signature = _stored(monkeypatch, doc)
    with pytest.raises(contract.ReportVerificationError) as exc:
        contract.verify_stored_report(header, body=body, signature=signature, rows=rows)
    assert exc.value.reason == "rows_tampered"


def test_verify_accepts_an_explicit_zero_score_row(monkeypatch):
    """A zero-scored miner is legitimate, so score_count counting it is not a
    row-count mismatch."""
    doc = _document(scores={"5Alice": 3.0, "5Zero": 0.0})
    header, body, signature = _stored(monkeypatch, doc)
    out = contract.verify_stored_report(
        header, body=body, signature=signature, rows={"5Alice": 3.0, "5Zero": 0.0}
    )
    assert out["scores"] == {"5Alice": 3.0, "5Zero": 0.0}


def test_verify_rejects_a_non_numeric_score(monkeypatch):
    monkeypatch.setenv(contract.HMAC_SECRET_ENV, SECRET)
    doc = _document(scores={"5Alice": "three"})
    body = contract.canonical_report_bytes(contract.semantic_view(doc))
    header = {
        "network": doc["network"], "netuid": doc["netuid"],
        "source_epoch": doc["source_epoch"], "producer_hotkey": doc["producer_hotkey"],
        "complete": 1, "generated_at_iso": doc["generated_at"],
        "report_sha256": contract.report_digest(doc),
        "body_sha256": hashlib.sha256(body).hexdigest(), "score_count": 1,
    }
    signature = "sha256=" + contract.body_hmac_hex(body, SECRET)
    with pytest.raises(contract.ReportVerificationError) as exc:
        contract.verify_stored_report(header, body=body, signature=signature)
    assert exc.value.reason == "malformed_report"


def test_a_forged_body_without_the_secret_cannot_be_made_to_verify(monkeypatch):
    """The property the whole read-side check rests on: an attacker with database
    write access but no secret can produce self-consistent digests, and still
    fails the HMAC step."""
    monkeypatch.setenv(contract.HMAC_SECRET_ENV, SECRET)
    doc = _document(scores={"5Attacker": 1000.0})
    body = contract.canonical_report_bytes(contract.semantic_view(doc))
    header = {
        "network": doc["network"], "netuid": doc["netuid"],
        "source_epoch": doc["source_epoch"], "producer_hotkey": doc["producer_hotkey"],
        "complete": 1, "generated_at_iso": doc["generated_at"],
        "report_sha256": contract.report_digest(doc),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "score_count": 1,
    }
    forged_signature = "sha256=" + contract.body_hmac_hex(body, "a-guessed-secret")
    with pytest.raises(contract.ReportVerificationError) as exc:
        contract.verify_stored_report(header, body=body, signature=forged_signature)
    assert exc.value.reason == "signature_invalid"


def test_the_ingest_module_reuses_this_contract():
    """One definition, not two: a copy would eventually drift and the drift would
    look like a passing suite."""
    from scaffold.publisher import cybergym_ingest as ingest

    assert ingest.canonical_report_bytes is contract.canonical_report_bytes
    assert ingest.report_digest is contract.report_digest
    assert ingest.receipt_id is contract.receipt_id
    assert ingest.verify_body_hmac is contract.verify_body_hmac
    assert set(ingest._ALLOWED_KEYS) == set(contract.SEMANTIC_KEYS)


def test_canonical_body_round_trips_through_json():
    doc = contract.semantic_view(_document())
    assert json.loads(contract.canonical_report_bytes(doc).decode("utf-8")) == doc
