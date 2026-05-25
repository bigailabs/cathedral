"""Contract tests for the PR2 SAT-only ``/v1/agents/submit`` shape.

Per recovery-plan Decision 1 (Option A), ``card_id`` survives on the
wire as a task-family discriminator with one accepted value:
``synthetic_boolean_v1``. Anything else (eu-ai-act, us-ai-eo,
uk-ai-whitepaper, singapore-pdpc, japan-meti-mic) gets HTTP 400 with a
pointer to skill.md.

Attestation modes ``tee`` and ``unverified`` were card-era only and
return HTTP 400 too. Only ``ssh-probe`` is accepted.

The ``bundle`` multipart field is OPTIONAL — SAT miners don't ship a
card bundle and the publisher never decrypts / validates / stores the
bytes. We still hash whatever's uploaded so the sr25519 claim signature
can verify against ``bundle_hash``.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Iterator

import blake3
import pytest
from bittensor_wallet import Keypair
from fastapi.testclient import TestClient

from cathedral.publisher.app import build_app
from cathedral.types import canonical_json_for_signing


_SAT_CARD_ID = "synthetic_boolean_v1"
_SSH_PROBE = "ssh-probe"

_LEGACY_CARD_IDS = (
    "eu-ai-act",
    "us-ai-eo",
    "uk-ai-whitepaper",
    "singapore-pdpc",
    "japan-meti-mic",
)


@pytest.fixture
def alice_keypair() -> Keypair:
    return Keypair.create_from_uri("//Alice")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = build_app(database_path=str(tmp_path / "publisher.db"))
    with TestClient(app) as c:
        yield c


def _sign(
    keypair: Keypair, *, bundle_hash: str, card_id: str, submitted_at: str
) -> str:
    payload = {
        "bundle_hash": bundle_hash,
        "card_id": card_id,
        "miner_hotkey": keypair.ss58_address,
        "submitted_at": submitted_at,
    }
    sig = keypair.sign(canonical_json_for_signing(payload))
    return base64.b64encode(sig).decode("ascii")


def _post(
    client: TestClient,
    *,
    keypair: Keypair,
    card_id: str = _SAT_CARD_ID,
    attestation_mode: str = _SSH_PROBE,
    display_name: str = "alice-sat-box",
    ssh_host: str | None = "alice.example.com",
    ssh_user: str | None = "cathedral",
    ssh_port: int | None = 22,
    bundle: bytes | None = None,
    submitted_at: str | None = None,
    extra_data: dict[str, Any] | None = None,
) -> Any:
    submitted_at = submitted_at or _now_iso_ms()
    bundle_bytes = bundle if bundle is not None else b""
    bundle_hash = blake3.blake3(bundle_bytes).hexdigest()
    sig = _sign(
        keypair,
        bundle_hash=bundle_hash,
        card_id=card_id,
        submitted_at=submitted_at,
    )
    data: dict[str, Any] = {
        "card_id": card_id,
        "display_name": display_name,
        "attestation_mode": attestation_mode,
        "submitted_at": submitted_at,
    }
    if ssh_host is not None:
        data["ssh_host"] = ssh_host
    if ssh_user is not None:
        data["ssh_user"] = ssh_user
    if ssh_port is not None:
        data["ssh_port"] = str(ssh_port)
    if extra_data:
        data.update(extra_data)
    headers = {
        "X-Cathedral-Hotkey": keypair.ss58_address,
        "X-Cathedral-Signature": sig,
    }
    files: dict[str, Any] = {}
    if bundle is not None:
        files["bundle"] = ("agent.zip", bundle, "application/zip")
    return client.post(
        "/v1/agents/submit", headers=headers, data=data, files=files or None
    )


def _now_iso_ms() -> str:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# --------------------------------------------------------------------------
# Happy path — minimal SAT registration
# --------------------------------------------------------------------------


def test_sat_registration_returns_202_pending_check(
    client: TestClient, alice_keypair: Keypair
) -> None:
    """Valid SAT registration with no bundle returns 202 + pending_check."""
    r = _post(client, keypair=alice_keypair)
    assert r.status_code == 202, f"expected 202, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["status"] == "pending_check", body
    assert "id" in body
    assert "submitted_at" in body
    assert body["submitted_at"].endswith("Z"), (
        f"submitted_at must end with 'Z', got {body['submitted_at']!r}"
    )


def test_sat_registration_with_ignored_bundle_still_202(
    client: TestClient, alice_keypair: Keypair
) -> None:
    """A bundle field is accepted but ignored as a card; submission still
    succeeds. The signed claim covers the bundle's blake3 so signature
    verification has something to bind to."""
    junk_bundle = b"PK\x03\x04" + b"not really a card bundle anymore"
    r = _post(client, keypair=alice_keypair, bundle=junk_bundle)
    assert r.status_code == 202, f"expected 202, got {r.status_code}: {r.text}"
    assert r.json()["status"] == "pending_check"


def test_sat_registration_persists_ssh_coordinates(
    client: TestClient, alice_keypair: Keypair, tmp_path: Path
) -> None:
    """ssh_host / ssh_user / ssh_port are persisted onto the row so the
    SAT prober loop can find the miner's box."""
    r = _post(
        client,
        keypair=alice_keypair,
        ssh_host="box.example.org",
        ssh_user="cathedral",
        ssh_port=2200,
    )
    assert r.status_code == 202
    submission_id = r.json()["id"]

    # Pull the row directly to confirm the SAT shape persisted. We sneak
    # onto the same aiosqlite connection the app is using by routing the
    # query through a sync sqlite3 read on the underlying DB file path —
    # this avoids the cross-thread event-loop dance that TestClient +
    # asyncio make tricky.
    import sqlite3

    db_path = str(tmp_path / "publisher.db")
    with sqlite3.connect(db_path) as raw:
        raw.row_factory = sqlite3.Row
        row = raw.execute(
            "SELECT * FROM agent_submissions WHERE id = ?",
            (submission_id,),
        ).fetchone()
    assert row is not None
    sub = dict(row)
    assert sub is not None
    assert sub["card_id"] == _SAT_CARD_ID
    assert sub["attestation_mode"] == _SSH_PROBE
    assert sub["ssh_host"] == "box.example.org"
    assert sub["ssh_user"] == "cathedral"
    assert sub["ssh_port"] == 2200
    assert sub["status"] == "pending_check"


# --------------------------------------------------------------------------
# Legacy card_id rejection
# --------------------------------------------------------------------------


@pytest.mark.parametrize("legacy_card_id", _LEGACY_CARD_IDS)
def test_legacy_card_ids_rejected_with_migration_pointer(
    client: TestClient, alice_keypair: Keypair, legacy_card_id: str
) -> None:
    """Card-era ``card_id`` values 400 with a skill.md pointer."""
    r = _post(client, keypair=alice_keypair, card_id=legacy_card_id)
    assert r.status_code == 400, (
        f"card_id={legacy_card_id!r} should 400; got {r.status_code}: {r.text}"
    )
    detail = r.json().get("detail", "")
    assert "synthetic_boolean_v1" in detail, (
        f"400 body should name the SAT card_id; got {detail!r}"
    )
    assert "skill.md" in detail, f"400 body should point at skill.md; got {detail!r}"


# --------------------------------------------------------------------------
# Attestation mode rejection
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["tee", "unverified", "polaris", "polaris-deploy", "bundle"])
def test_legacy_attestation_modes_rejected(
    client: TestClient, alice_keypair: Keypair, mode: str
) -> None:
    """tee / unverified / polaris* all 400 with a migration pointer."""
    r = _post(client, keypair=alice_keypair, attestation_mode=mode)
    assert r.status_code == 400, (
        f"attestation_mode={mode!r} should 400; got {r.status_code}: {r.text}"
    )
    detail = r.json().get("detail", "")
    assert "ssh-probe" in detail, (
        f"400 body should name ssh-probe; got {detail!r}"
    )
    assert "skill.md" in detail, f"400 body should point at skill.md; got {detail!r}"


# --------------------------------------------------------------------------
# Required SSH coordinates
# --------------------------------------------------------------------------


def test_missing_ssh_host_rejected(
    client: TestClient, alice_keypair: Keypair
) -> None:
    r = _post(client, keypair=alice_keypair, ssh_host=None)
    assert r.status_code == 400, r.text


def test_missing_ssh_user_rejected(
    client: TestClient, alice_keypair: Keypair
) -> None:
    r = _post(client, keypair=alice_keypair, ssh_user=None)
    assert r.status_code == 400, r.text


def test_default_ssh_port_22_when_omitted(
    client: TestClient, alice_keypair: Keypair
) -> None:
    """ssh_port defaults to 22 when the caller omits it."""
    r = _post(client, keypair=alice_keypair, ssh_port=None)
    assert r.status_code == 202, r.text


def test_ssh_port_out_of_range_rejected(
    client: TestClient, alice_keypair: Keypair
) -> None:
    r = _post(client, keypair=alice_keypair, ssh_port=99999)
    assert r.status_code == 400, r.text


# --------------------------------------------------------------------------
# Signature / auth
# --------------------------------------------------------------------------


def test_missing_signature_headers_401(
    client: TestClient, alice_keypair: Keypair
) -> None:
    r = client.post(
        "/v1/agents/submit",
        data={
            "card_id": _SAT_CARD_ID,
            "display_name": "alice",
            "attestation_mode": _SSH_PROBE,
            "ssh_host": "x.example.com",
            "ssh_user": "cathedral",
        },
    )
    assert r.status_code == 401, r.text


def test_bad_signature_returns_401(
    client: TestClient, alice_keypair: Keypair
) -> None:
    """A well-formed but wrong signature 401s."""
    submitted_at = _now_iso_ms()
    headers = {
        "X-Cathedral-Hotkey": alice_keypair.ss58_address,
        # 64-byte garbage, base64-encoded — survives the auth-header
        # length gate but won't verify.
        "X-Cathedral-Signature": base64.b64encode(b"\x00" * 64).decode(),
    }
    r = client.post(
        "/v1/agents/submit",
        headers=headers,
        data={
            "card_id": _SAT_CARD_ID,
            "display_name": "alice",
            "attestation_mode": _SSH_PROBE,
            "submitted_at": submitted_at,
            "ssh_host": "x.example.com",
            "ssh_user": "cathedral",
        },
    )
    assert r.status_code == 401, r.text
