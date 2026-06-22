"""Smoke checks for the solver-attestation publisher surface.

This does not perform real DCAP verification. It proves the default-off route
posture, DB migrations, nonce binding, replay rejection, and the shadow/test
stub path around scaffold.publisher.attest.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

from scaffold import wire
from scaffold.publisher import app as app_mod
from scaffold.publisher import rows
from scaffold.publisher.keys import generate_test_key
from scaffold.publisher.store import Store


@contextmanager
def _clean_env():
    keys = [
        "DATABASE_URL",
        "CATHEDRAL_ATTEST_ENABLED",
        "CATHEDRAL_ATTEST_ALLOW_STUB",
        "CATHEDRAL_ATTEST_DCAP_VERIFY_CMD",
        "CATHEDRAL_ATTEST_DCAP_TIMEOUT_SECS",
        "CATHEDRAL_ATTEST_MULTIPLIER",
        "CATHEDRAL_ATTEST_STATUS_TOKEN",
        "CATHEDRAL_ATTEST_STATUS_PUBLIC",
        "CATHEDRAL_ENV",
        "CATHEDRAL_PRODUCTION",
    ]
    old = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _now_iso() -> str:
    d = datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


class AcceptVerifier:
    backend = "accept-test-verifier"

    def verify(self, ss58_address: str, message: bytes, signature_b64: str) -> bool:
        return signature_b64 == "ok"


def _quote_b64(nonce: str, miner_pubkey_b64: str, solver_digest: str, receipt: str) -> str:
    lo = hashlib.sha256((nonce + miner_pubkey_b64).encode()).digest()
    receipt_sha = hashlib.sha256(receipt.encode()).hexdigest()
    hi = hashlib.sha256((solver_digest + receipt_sha).encode()).digest()
    quote = bytearray(632)
    quote[568:632] = lo + hi
    return base64.b64encode(bytes(quote)).decode("ascii")


def _seed_eval_row(
    store: Store,
    *,
    key_hex: str,
    miner_hotkey: str,
    row_id: str,
    challenge_id: str = "sat-attest-1",
    answer_hash: str = "aa" * 32,
    weighted_score: float = 0.4,
) -> None:
    row = rows.build_solve_rows(
        row_uuid=row_id,
        miner_hotkey=miner_hotkey,
        agent_id="agent-attest-smoke",
        challenge_id=challenge_id,
        tier=1,
        weighted_score=weighted_score,
        answer_hash=answer_hash,
        verifier_details_hash="bb" * 32,
        ran_at=_now_iso(),
        epoch_salt="epoch_attest_smoke:synthetic_boolean_v1",
        solve_rank=1,
        solved=True,
        private_key_hex=key_hex,
    )[0]
    assert store.insert_row(row) == 1


def main() -> None:
    assert app_mod._parse_iso("2026-06-20T12:00:00") == app_mod._parse_iso(
        "2026-06-20T12:00:00Z")
    try:
        from fastapi.testclient import TestClient
    except Exception as e:
        raise RuntimeError(f"attestation route smoke requires FastAPI TestClient: {e}") from e

    with _clean_env():
        key_hex = generate_test_key()
        pub_hex = rows.public_key_hex(key_hex)

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        store = Store(db_path)
        try:
            tables = {
                row["name"]
                for row in store.query(
                    "SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert "attest_nonces" in tables
            assert "attestations" in tables
            nonce_cols = {
                row["name"]
                for row in store.query("PRAGMA table_info(attest_nonces)")
            }
            assert "miner_pubkey_b64" in nonce_cols
            eval_cols = {
                row["name"]
                for row in store.query("PRAGMA table_info(eval_runs)")
            }
            assert "attested" in eval_cols
            from scaffold.publisher import attest as attest_mod
            no_verifier = attest_mod.verify_attestation(
                store, {}, private_key_hex=key_hex)
            assert no_verifier.ok is False
            assert no_verifier.reason == "no_verifier_configured"
            os.environ["CATHEDRAL_ATTEST_ALLOW_STUB"] = "1"
            assert isinstance(attest_mod.configured_intel_verifier(), attest_mod.StubIntelVerifier)
            os.environ["CATHEDRAL_ENV"] = "production"
            assert attest_mod.configured_intel_verifier() is None
            os.environ.pop("CATHEDRAL_ENV", None)
            os.environ.pop("CATHEDRAL_ATTEST_ALLOW_STUB", None)
        finally:
            store.close()
            try:
                os.unlink(db_path)
            except OSError:
                pass

        original_verifier = app_mod.default_verifier
        app_mod.default_verifier = lambda: AcceptVerifier()
        try:
            app = app_mod.build_app(database_path=":memory:", signing_key_hex=key_hex)
            with TestClient(app) as client:
                headers = {
                    "X-Cathedral-Hotkey": "5MinerA",
                    "X-Cathedral-Signature": "ok",
                    "X-Cathedral-Submitted-At": _now_iso(),
                }
                assert client.post(
                    "/v1/attest/nonce",
                    data={"challenge_id": "sat-attest-1", "miner_pubkey_b64": "pub-a"},
                    headers=headers,
                ).status_code == 404

            os.environ["CATHEDRAL_ATTEST_ENABLED"] = "1"
            app = app_mod.build_app(database_path=":memory:", signing_key_hex=key_hex)
            with TestClient(app) as client:
                headers = {
                    "X-Cathedral-Hotkey": "5MinerA",
                    "X-Cathedral-Signature": "ok",
                    "X-Cathedral-Submitted-At": _now_iso(),
                }
                empty_pubkey = client.post(
                    "/v1/attest/nonce",
                    data={
                        "challenge_id": "sat-attest-1",
                        "miner_pubkey_b64": "",
                        "ttl_secs": "60",
                    },
                    headers=headers,
                )
                assert empty_pubkey.status_code in {400, 422}, empty_pubkey.text
                if empty_pubkey.status_code == 400:
                    assert empty_pubkey.json()["detail"] == "missing_miner_pubkey_b64"
                nresp = client.post(
                    "/v1/attest/nonce",
                    data={
                        "challenge_id": "sat-attest-1",
                        "miner_pubkey_b64": "pub-a",
                        "ttl_secs": "60",
                    },
                    headers=headers,
                )
                assert nresp.status_code == 200, nresp.text
                nonce = nresp.json()["nonce"]
                assert len(nonce) == 64
                assert client.post("/v1/attest", json={}).status_code == 503

            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as vf:
                vf.write(
                    "import json,sys\n"
                    "_quote,_report_data,out=sys.argv[1:4]\n"
                    "json.dump({'intel_verified': True, "
                    "'report_data_match': len(_report_data) == 128}, open(out,'w'))\n")
                verifier_script = vf.name
            os.environ["CATHEDRAL_ATTEST_DCAP_VERIFY_CMD"] = (
                f"{sys.executable} {verifier_script}")
            os.environ["CATHEDRAL_ATTEST_MULTIPLIER"] = "2.0"
            app = app_mod.build_app(database_path=":memory:", signing_key_hex=key_hex)
            store = app.state.store
            _seed_eval_row(
                store, key_hex=key_hex, miner_hotkey="5MinerA",
                row_id="attest-row-1")
            _seed_eval_row(
                store, key_hex=key_hex, miner_hotkey="5MinerA",
                row_id="attest-row-other-answer", answer_hash="ff" * 32)
            _seed_eval_row(
                store, key_hex=key_hex, miner_hotkey="5MinerA",
                row_id="attest-row-other-challenge", challenge_id="sat-other-1")
            with TestClient(app) as client:
                headers = {
                    "X-Cathedral-Hotkey": "5MinerA",
                    "X-Cathedral-Signature": "ok",
                    "X-Cathedral-Submitted-At": _now_iso(),
                }
                nresp = client.post(
                    "/v1/attest/nonce",
                    data={
                        "challenge_id": "sat-attest-1",
                        "miner_pubkey_b64": "pub-a",
                        "ttl_secs": "60",
                    },
                    headers=headers,
                )
                assert nresp.status_code == 200, nresp.text
                nonce = nresp.json()["nonce"]
                solver_digest = "sha256:" + "11" * 32
                receipt = json.dumps({
                    "challenge_id": "sat-attest-1",
                    "instance_sha256": "cc" * 32,
                    "solver_digest": solver_digest,
                    "wall_ms": 123,
                    "outcome": "SAT",
                    "answer_hash": "aa" * 32,
                    "miner_pubkey": "pub-a",
                }, separators=(",", ":"))
                payload = {
                    "quote_b64": _quote_b64(nonce, "pub-a", solver_digest, receipt),
                    "collateral": {},
                    "nonce": nonce,
                    "miner_pubkey_b64": "pub-a",
                    "solver_digest": solver_digest,
                    "receipt": receipt,
                    "challenge_id": "sat-attest-1",
                    "instance_sha256": "cc" * 32,
                    "outcome": "SAT",
                    "answer_hash": "aa" * 32,
                    "eval_run_id": "attest-row-1",
                }
                nresp_bad_challenge = client.post(
                    "/v1/attest/nonce",
                    data={
                        "challenge_id": "sat-attest-1",
                        "miner_pubkey_b64": "pub-a",
                        "ttl_secs": "60",
                    },
                    headers=headers,
                )
                receipt_bad_challenge = json.dumps({
                    "challenge_id": "sat-attest-other",
                    "instance_sha256": "cc" * 32,
                    "solver_digest": solver_digest,
                    "wall_ms": 123,
                    "outcome": "SAT",
                    "answer_hash": "aa" * 32,
                    "miner_pubkey": "pub-a",
                }, separators=(",", ":"))
                bad_challenge_payload = dict(payload)
                bad_challenge_payload["nonce"] = nresp_bad_challenge.json()["nonce"]
                bad_challenge_payload["receipt"] = receipt_bad_challenge
                bad_challenge_payload["quote_b64"] = _quote_b64(
                    bad_challenge_payload["nonce"], "pub-a", solver_digest,
                    receipt_bad_challenge)
                bad_challenge = client.post(
                    "/v1/attest", json=bad_challenge_payload, headers=headers)
                assert bad_challenge.status_code == 200, bad_challenge.text
                assert bad_challenge.json()["ok"] is False
                assert bad_challenge.json()["reason"] == "receipt_challenge_mismatch"

                unsigned = client.post("/v1/attest", json=payload)
                assert unsigned.status_code == 401, unsigned.text
                assert unsigned.json()["detail"] == "missing_hotkey_signature"
                bad_sig_headers = dict(headers)
                bad_sig_headers["X-Cathedral-Signature"] = "bad"
                bad_sig = client.post(
                    "/v1/attest", json=payload, headers=bad_sig_headers)
                assert bad_sig.status_code == 401, bad_sig.text
                wrong_hotkey_headers = dict(headers)
                wrong_hotkey_headers["X-Cathedral-Hotkey"] = "5MinerB"
                wrong_hotkey = client.post(
                    "/v1/attest", json=payload, headers=wrong_hotkey_headers)
                assert wrong_hotkey.status_code == 200, wrong_hotkey.text
                assert wrong_hotkey.json()["ok"] is False
                assert wrong_hotkey.json()["reason"] == "authenticated_hotkey_mismatch"

                vresp = client.post("/v1/attest", json=payload, headers=headers)
                assert vresp.status_code == 200, vresp.text
                assert vresp.json()["ok"] is True, vresp.text
                assert vresp.json()["verifier_backend"] == "command-dcap"

                upgraded = store.query(
                    "SELECT row_json, attested FROM eval_runs WHERE id=?",
                    ("attest-row-1",))[0]
                row = json.loads(upgraded["row_json"])
                assert upgraded["attested"] == 1
                assert row["attested"] is True
                assert row["weighted_score"] == 0.8
                assert wire.verify_row(row, pub_hex)

                status = client.get("/v1/attest/status/attest-row-1")
                assert status.status_code == 503, status.text
                os.environ["CATHEDRAL_ATTEST_STATUS_TOKEN"] = "status-secret"
                status = client.get("/v1/attest/status/attest-row-1")
                assert status.status_code == 401, status.text
                status = client.get(
                    "/v1/attest/status/attest-row-1",
                    headers={"Authorization": "Bearer status-secret"},
                )
                assert status.status_code == 200, status.text
                assert status.json()["attested"] is True
                assert len(status.json()["attestations"]) == 1
                audit = store.query(
                    "SELECT quote_digest, receipt_digest, verifier_backend "
                    "FROM attestations WHERE eval_run_id=?",
                    ("attest-row-1",))[0]
                assert audit["quote_digest"] == hashlib.sha256(
                    base64.b64decode(payload["quote_b64"])).hexdigest()
                assert audit["receipt_digest"] == hashlib.sha256(
                    receipt.encode()).hexdigest()
                assert audit["verifier_backend"] == "command-dcap"

                replay = client.post("/v1/attest", json=payload, headers=headers)
                assert replay.status_code == 200, replay.text
                assert replay.json()["ok"] is False
                assert replay.json()["reason"] == "nonce_already_used"

                nresp2 = client.post(
                    "/v1/attest/nonce",
                    data={
                        "challenge_id": "sat-attest-1",
                        "miner_pubkey_b64": "pub-a",
                        "ttl_secs": "60",
                    },
                    headers=headers,
                )
                payload2 = dict(payload)
                payload2["nonce"] = nresp2.json()["nonce"]
                payload2["quote_b64"] = _quote_b64(
                    payload2["nonce"], "pub-a", solver_digest, receipt)
                second = client.post("/v1/attest", json=payload2, headers=headers)
                assert second.status_code == 200, second.text
                assert second.json()["ok"] is False
                assert second.json()["reason"] == "already_attested"

                nresp3 = client.post(
                    "/v1/attest/nonce",
                    data={
                        "challenge_id": "sat-attest-1",
                        "miner_pubkey_b64": "pub-a",
                        "ttl_secs": "60",
                    },
                    headers=headers,
                )
                redirected = dict(payload)
                redirected["nonce"] = nresp3.json()["nonce"]
                redirected["quote_b64"] = _quote_b64(
                    redirected["nonce"], "pub-a", solver_digest, receipt)
                redirected["eval_run_id"] = "attest-row-other-answer"
                denied_redirect = client.post(
                    "/v1/attest", json=redirected, headers=headers)
                assert denied_redirect.status_code == 200, denied_redirect.text
                assert denied_redirect.json()["ok"] is False
                assert denied_redirect.json()["reason"] == "eval_run_answer_mismatch"

                nresp4 = client.post(
                    "/v1/attest/nonce",
                    data={
                        "challenge_id": "sat-attest-1",
                        "miner_pubkey_b64": "pub-a",
                        "ttl_secs": "60",
                    },
                    headers=headers,
                )
                redirected2 = dict(payload)
                redirected2["nonce"] = nresp4.json()["nonce"]
                redirected2["quote_b64"] = _quote_b64(
                    redirected2["nonce"], "pub-a", solver_digest, receipt)
                redirected2["eval_run_id"] = "attest-row-other-challenge"
                denied_challenge = client.post(
                    "/v1/attest", json=redirected2, headers=headers)
                assert denied_challenge.status_code == 200, denied_challenge.text
                assert denied_challenge.json()["ok"] is False
                assert denied_challenge.json()["reason"] == "eval_run_challenge_mismatch"

                nresp5 = client.post(
                    "/v1/attest/nonce",
                    data={
                        "challenge_id": "sat-attest-1",
                        "miner_pubkey_b64": "pub-a",
                        "ttl_secs": "60",
                    },
                    headers=headers,
                )
                pubkey_mismatch = dict(payload)
                pubkey_mismatch["nonce"] = nresp5.json()["nonce"]
                pubkey_mismatch["miner_pubkey_b64"] = "pub-b"
                pubkey_mismatch["quote_b64"] = _quote_b64(
                    pubkey_mismatch["nonce"], "pub-b", solver_digest, receipt)
                denied_pubkey = client.post(
                    "/v1/attest", json=pubkey_mismatch, headers=headers)
                assert denied_pubkey.status_code == 200, denied_pubkey.text
                assert denied_pubkey.json()["ok"] is False
                assert denied_pubkey.json()["reason"] == "nonce_pubkey_mismatch"

            app2 = app_mod.build_app(database_path=":memory:", signing_key_hex=key_hex)
            store2 = app2.state.store
            _seed_eval_row(
                store2, key_hex=key_hex, miner_hotkey="5MinerB",
                row_id="attest-row-2")
            with TestClient(app2) as client:
                headers = {
                    "X-Cathedral-Hotkey": "5MinerA",
                    "X-Cathedral-Signature": "ok",
                    "X-Cathedral-Submitted-At": _now_iso(),
                }
                nresp = client.post(
                    "/v1/attest/nonce",
                    data={
                        "challenge_id": "sat-attest-1",
                        "miner_pubkey_b64": "pub-a",
                        "ttl_secs": "60",
                    },
                    headers=headers,
                )
                nonce = nresp.json()["nonce"]
                solver_digest = "sha256:" + "22" * 32
                receipt = json.dumps({
                    "challenge_id": "sat-attest-1",
                    "instance_sha256": "dd" * 32,
                    "solver_digest": solver_digest,
                    "wall_ms": 123,
                    "outcome": "SAT",
                    "answer_hash": "ee" * 32,
                }, separators=(",", ":"))
                bad_identity_payload = {
                    "quote_b64": _quote_b64(nonce, "pub-a", solver_digest, receipt),
                    "collateral": {},
                    "nonce": nonce,
                    "miner_pubkey_b64": "pub-a",
                    "solver_digest": solver_digest,
                    "receipt": receipt,
                    "challenge_id": "sat-attest-1",
                    "instance_sha256": "dd" * 32,
                    "outcome": "SAT",
                    "answer_hash": "ee" * 32,
                    "eval_run_id": "attest-row-2",
                }
                denied = client.post(
                    "/v1/attest", json=bad_identity_payload, headers=headers)
                assert denied.status_code == 200, denied.text
                assert denied.json()["ok"] is False
                assert denied.json()["reason"] == "nonce_miner_mismatch"
            try:
                os.unlink(verifier_script)
            except OSError:
                pass
        finally:
            app_mod.default_verifier = original_verifier

    print("attest_verify: ok")


if __name__ == "__main__":
    main()
