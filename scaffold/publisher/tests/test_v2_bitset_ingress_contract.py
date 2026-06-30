from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from scaffold.publisher.auth import BittensorVerifier
from scaffold.publisher import v2_bitset_submit


ROOT = Path(__file__).resolve().parents[3]
GOLDEN = ROOT / "deploy" / "golden" / "v2_bitset_ingress_golden.json"


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * ((4 - len(text) % 4) % 4))


def test_v2_bitset_ingress_golden_contract():
    vector = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert vector["schema"] == "cathedral.v2.bitset_ingress_golden.v1"
    assert "not-live" in vector["fake_submit_token_secret"]

    token = vector["submit_token"]
    token_parts = token.split(".")
    assert token_parts[0] == "v1"
    assert len(token_parts) == 3

    token_payload_bytes = _b64url_decode(token_parts[1])
    token_sig_bytes = _b64url_decode(token_parts[2])
    assert token_payload_bytes.decode("utf-8") == vector["token_payload_canonical_json"]
    assert hashlib.sha256(token_payload_bytes).hexdigest() == vector["token_payload_sha256"]
    assert hashlib.sha256(token_sig_bytes).hexdigest() == vector["token_signature_sha256"]

    token_payload = v2_bitset_submit.verify_submit_token(
        token,
        secret=vector["fake_submit_token_secret"],
        miner_hotkey=vector["miner_hotkey"],
        challenge_id=vector["normalized_submit_body"]["challenge_id"],
    )
    for key, value in vector["token_payload"].items():
        assert token_payload[key] == value

    submit = v2_bitset_submit.normalize_submit_body(
        vector["normalized_submit_body"],
        miner_hotkey=vector["miner_hotkey"],
        submitted_at=vector["headers"]["X-Cathedral-Submitted-At"],
        card_id="synthetic_boolean_v1",
    )
    assert submit == vector["normalized_submit_body"]
    canonical_submit = v2_bitset_submit.canonical_submit_bytes(submit)
    assert canonical_submit.decode("utf-8") == vector["canonical_submit_json"]
    assert hashlib.sha256(canonical_submit).hexdigest() == vector["canonical_submit_sha256"]

    assert BittensorVerifier().verify(
        vector["miner_hotkey"],
        canonical_submit,
        vector["signature_b64"],
    )

    raw, assignment = v2_bitset_submit.decode_assignment_b64(
        vector["assignment_b64"],
        nvars=int(vector["token_payload"]["nvars"]),
    )
    assert raw.hex() == vector["assignment_raw_hex"]
    assert assignment == vector["assignment"]
    assert hashlib.sha256(raw).hexdigest() == vector["assignment_sha256"]

    assert v2_bitset_submit.idempotency_key(
        miner_hotkey=vector["miner_hotkey"],
        challenge_id=vector["normalized_submit_body"]["challenge_id"],
    ) == vector["idempotency_key"]
