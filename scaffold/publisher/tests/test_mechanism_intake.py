from __future__ import annotations

import base64
import json
import time

import pytest
from bittensor_wallet import Keypair
from fastapi import FastAPI
from starlette.testclient import TestClient

from scaffold.publisher import mechanism_intake
from scaffold.publisher.mechanism_router import MechanismSpec, ScoreVector, ScoreVectorMeta


class FakeMechanismStore:
    """Minimal in-memory MechanismStore for tests (not the real impl)."""

    def __init__(self, specs: dict[str, MechanismSpec] | None = None) -> None:
        self.specs: dict[str, MechanismSpec] = dict(specs or {})
        self.puts: list[tuple[str, ScoreVector, ScoreVectorMeta]] = []

    def list_specs(self) -> list[MechanismSpec]:
        return list(self.specs.values())

    def get_spec(self, mechanism_id: str):
        return self.specs.get(mechanism_id)

    def upsert_spec(self, spec: MechanismSpec) -> None:
        self.specs[spec.mechanism_id] = spec

    def put_scores(self, mechanism_id: str, scores: ScoreVector, meta: ScoreVectorMeta) -> None:
        self.puts.append((mechanism_id, dict(scores), meta))

    def get_scores(self, mechanism_id: str):
        for mid, scores, meta in reversed(self.puts):
            if mid == mechanism_id:
                return dict(scores), meta
        return None


OWNER = Keypair.create_from_uri("//MechanismIntakeOwner")
NOT_OWNER = Keypair.create_from_uri("//MechanismIntakeNotOwner")
MECHANISM_ID = "sat_v1"


def _spec(owner=OWNER) -> MechanismSpec:
    return MechanismSpec(
        mechanism_id=MECHANISM_ID,
        owner_pubkey=owner.ss58_address,
        weight_fraction=0.1,
        tier="signed",
    )


def _body(*, mechanism_id: str = MECHANISM_ID, scores: dict[str, float] | None = None,
          signed_at_ms: int | None = None) -> dict:
    return {
        "mechanism_id": mechanism_id,
        "scores": scores if scores is not None else {"7": 1.5, "12": 0.0},
        "signed_at_ms": signed_at_ms if signed_at_ms is not None else int(time.time() * 1000),
    }


def _sign(kp: Keypair, body: dict) -> str:
    canonical = mechanism_intake.canonical_score_post_bytes(body)
    return base64.b64encode(kp.sign(canonical)).decode("ascii")


def _headers(kp: Keypair, sig_b64: str) -> dict:
    return {"X-Cathedral-Hotkey": kp.ss58_address, "X-Cathedral-Signature": sig_b64}


@pytest.fixture
def store():
    s = FakeMechanismStore({MECHANISM_ID: _spec()})
    mechanism_intake.set_mechanism_store(s)
    yield s
    mechanism_intake.set_mechanism_store(None)


@pytest.fixture
def client(monkeypatch, store):
    monkeypatch.setenv(mechanism_intake.INTAKE_ENABLED_ENV, "1")
    app = FastAPI()
    app.include_router(mechanism_intake.router)
    return TestClient(app)


def test_owner_post_accepted_and_stored(client, store):
    body = _body()
    sig = _sign(OWNER, body)
    r = client.post(
        f"/mechanisms/{MECHANISM_ID}/scores",
        content=json.dumps(body),
        headers={**_headers(OWNER, sig), "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["accepted"] is True
    assert out["mechanism_id"] == MECHANISM_ID
    assert out["n_uids"] == 2
    assert out["signed_at_ms"] == body["signed_at_ms"]
    assert out["receipt_id"]

    assert len(store.puts) == 1
    mid, scores, meta = store.puts[0]
    assert mid == MECHANISM_ID
    assert scores == {7: 1.5, 12: 0.0}
    assert meta.mechanism_id == MECHANISM_ID
    assert meta.signed_at_ms == body["signed_at_ms"]
    assert meta.sig_ok is True
    assert meta.source == "signed_post"


def test_non_owner_signature_is_403(client, store):
    body = _body()
    sig = _sign(NOT_OWNER, body)
    r = client.post(
        f"/mechanisms/{MECHANISM_ID}/scores",
        content=json.dumps(body),
        headers={**_headers(NOT_OWNER, sig), "Content-Type": "application/json"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "not_mechanism_owner"
    assert store.puts == []


def test_bad_signature_is_401(client, store):
    body = _body()
    sig = _sign(OWNER, body)
    tampered_sig = sig[:-4] + ("AAAA" if sig[-4:] != "AAAA" else "BBBB")
    r = client.post(
        f"/mechanisms/{MECHANISM_ID}/scores",
        content=json.dumps(body),
        headers={**_headers(OWNER, tampered_sig), "Content-Type": "application/json"},
    )
    assert r.status_code == 401, r.text
    assert r.json()["detail"] == "invalid_signature"
    assert store.puts == []


def test_negative_score_is_400(client, store):
    body = _body(scores={"7": -1.0})
    sig = _sign(OWNER, body)
    r = client.post(
        f"/mechanisms/{MECHANISM_ID}/scores",
        content=json.dumps(body),
        headers={**_headers(OWNER, sig), "Content-Type": "application/json"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "invalid_score"
    assert store.puts == []


def test_malformed_body_missing_field_is_400(client, store):
    body = {"mechanism_id": MECHANISM_ID, "scores": {"7": 1.0}}  # missing signed_at_ms
    sig = _sign(OWNER, body)
    r = client.post(
        f"/mechanisms/{MECHANISM_ID}/scores",
        content=json.dumps(body),
        headers={**_headers(OWNER, sig), "Content-Type": "application/json"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "missing_fields"
    assert store.puts == []


def test_non_int_uid_key_is_400(client, store):
    body = _body(scores={"not-a-uid": 1.0})
    sig = _sign(OWNER, body)
    r = client.post(
        f"/mechanisms/{MECHANISM_ID}/scores",
        content=json.dumps(body),
        headers={**_headers(OWNER, sig), "Content-Type": "application/json"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "invalid_uid"
    assert store.puts == []


def test_oversize_body_is_413(monkeypatch, store):
    monkeypatch.setenv(mechanism_intake.INTAKE_ENABLED_ENV, "1")
    monkeypatch.setenv(mechanism_intake.MAX_BODY_BYTES_ENV, "200")
    app = FastAPI()
    app.include_router(mechanism_intake.router)
    client = TestClient(app)

    big_scores = {str(uid): 1.0 for uid in range(200)}
    body = _body(scores=big_scores)
    raw = json.dumps(body)
    assert len(raw.encode("utf-8")) > 200

    r = client.post(
        f"/mechanisms/{MECHANISM_ID}/scores",
        content=raw,
        headers={
            "X-Cathedral-Hotkey": OWNER.ss58_address,
            "X-Cathedral-Signature": "not-checked-because-oversize",
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 413, r.text
    assert store.puts == []


def test_disabled_by_default_is_404(store):
    app = FastAPI()
    app.include_router(mechanism_intake.router)
    client = TestClient(app)

    body = _body()
    sig = _sign(OWNER, body)
    r = client.post(
        f"/mechanisms/{MECHANISM_ID}/scores",
        content=json.dumps(body),
        headers={**_headers(OWNER, sig), "Content-Type": "application/json"},
    )
    assert r.status_code == 404, r.text
    assert store.puts == []


def test_unknown_mechanism_id_is_400(client, store):
    body = _body(mechanism_id="unknown_mech")
    sig = _sign(OWNER, body)
    r = client.post(
        "/mechanisms/unknown_mech/scores",
        content=json.dumps(body),
        headers={**_headers(OWNER, sig), "Content-Type": "application/json"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "unknown_mechanism_id"
    assert store.puts == []
