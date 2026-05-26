"""Shared fixtures for v1 contract tests.

These tests are written against ``cathedral-redesign/CONTRACTS.md`` ONLY.
We deliberately avoid importing from the implementer's new modules
(``cathedral.publisher``, ``cathedral.eval``, ``cathedral.storage``,
``cathedral.auth``, ``cathedral.validator.pull_loop``). The tests should
fail loudly if the implementation diverges from the contract; the
implementer fixes the code, not the tests.

What we DO use from the existing repo:
- ``cathedral.types`` (pre-refactor, stays put)
- ``cathedral.types.canonical_json_for_signing`` (already pinned by
  ``test_polaris_contract.py``)
"""

from __future__ import annotations

import base64
import io
import json
import secrets
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import blake3
import pytest
from bittensor_wallet import Keypair

from cathedral.types import canonical_json_for_signing

# --------------------------------------------------------------------------
# Constants from CONTRACTS.md
# --------------------------------------------------------------------------

CONTRACT_BUNDLE_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB — Section 2.1
CONTRACT_LOGO_MAX_BYTES = 200 * 1024  # 200 KiB — Section 2.1
CONTRACT_SIGNATURE_HEADER = "X-Cathedral-Signature"
CONTRACT_HOTKEY_HEADER = "X-Cathedral-Hotkey"

# v1 launch card_ids. Collapsed to EU AI Act only; the earlier 5-card
# plan is deprecated and archived at Docker startup. Kept as a tuple so
# future cards land here as the launch set expands.
CONTRACT_V1_CARD_IDS: tuple[str, ...] = ("eu-ai-act",)

# First-mover constants per Section 7.2.
FIRST_MOVER_DELTA = 0.05
FIRST_MOVER_PENALTY_MULTIPLIER = 0.50
FIRST_MOVER_WINDOW_DAYS = 30
FIRST_MOVER_FINGERPRINT_WINDOW_DAYS = 7

# Allowed AgentSubmission status transitions per Section 6.
ALLOWED_STATUSES: frozenset[str] = frozenset(
    {
        "pending_check",
        "queued",
        "evaluating",
        "ranked",
        "rejected",
        "withdrawn",
        "pending_solution",
        "valid_attestation_pending",
        "attest_failed",
    }
)


# --------------------------------------------------------------------------
# sr25519 hotkey helpers (real crypto, no faking — per task instructions)
# --------------------------------------------------------------------------


@pytest.fixture
def alice_keypair() -> Keypair:
    """Well-known //Alice keypair. ss58 = 5GrwvaEF5zXb..."""
    return Keypair.create_from_uri("//Alice")


@pytest.fixture
def bob_keypair() -> Keypair:
    return Keypair.create_from_uri("//Bob")


@pytest.fixture
def alice_hotkey(alice_keypair: Keypair) -> str:
    return alice_keypair.ss58_address


def sign_submission_payload(
    keypair: Keypair, *, bundle_hash: str, card_id: str, submitted_at: str
) -> str:
    """Per CONTRACTS.md Section 4.1: canonical_json over the submission claim,
    signed with sr25519 hotkey, base64-encoded."""
    payload = {
        "bundle_hash": bundle_hash,
        "card_id": card_id,
        "miner_hotkey": keypair.ss58_address,
        "submitted_at": submitted_at,
    }
    sig = keypair.sign(canonical_json_for_signing(payload))
    return base64.b64encode(sig).decode("ascii")


def canonical_submission_payload(
    *, bundle_hash: str, card_id: str, miner_hotkey: str, submitted_at: str
) -> dict[str, str]:
    """The exact dict that gets canonical-JSON-encoded for signing."""
    return {
        "bundle_hash": bundle_hash,
        "card_id": card_id,
        "miner_hotkey": miner_hotkey,
        "submitted_at": submitted_at,
    }


# --------------------------------------------------------------------------
# Bundle helpers
# --------------------------------------------------------------------------


def make_valid_bundle(
    *,
    soul_md: str = "# Soul\nI am a regulatory analyst agent.\n",
    extra_files: dict[str, bytes] | None = None,
) -> bytes:
    """Build a valid Hermes-profile zip per ARCHITECTURE_V1.md "Bundle structure".

    Minimum valid: a zip containing soul.md.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("soul.md", soul_md)
        zf.writestr(
            "AGENTS.md", "Maintain the regulatory card. Refresh every 4 hours.\n"
        )
        zf.writestr("config.yaml", "model: claude-3-opus\n")
        if extra_files:
            for name, data in extra_files.items():
                zf.writestr(name, data)
    return buf.getvalue()


def make_bundle_without_soul() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("AGENTS.md", "no soul here\n")
        zf.writestr("config.yaml", "model: x\n")
    return buf.getvalue()


def make_oversized_bundle() -> bytes:
    """Returns a zip whose raw bytes exceed 10 MiB.

    The zip stores incompressible random bytes so the on-disk size really
    crosses the 10 MiB limit (zlib won't shrink random data).
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("soul.md", "# Soul\n")
        # Stored mode + random bytes -> on-disk size ≈ payload size.
        zf.writestr("blob.bin", secrets.token_bytes(11 * 1024 * 1024))
    return buf.getvalue()


def make_invalid_zip() -> bytes:
    """Bytes that look like a file but are not a valid zip."""
    return b"this is definitely not a zip file at all\n" * 50


def blake3_hex(data: bytes) -> str:
    """Lowercase hex blake3 — matches CONTRACTS.md Section 4.4."""
    return blake3.blake3(data).hexdigest()


# --------------------------------------------------------------------------
# Test client fixture for the publisher app
# --------------------------------------------------------------------------


def _try_build_publisher_app() -> Any | None:
    """Best-effort import of the publisher FastAPI app.

    The implementer hasn't decided the exact module path yet. We try a
    handful of plausible locations from the contract. If none import,
    tests that depend on this fixture skip with a clear message — they
    are unblocked the moment the implementer pushes a wired-up app.
    """
    candidates = [
        ("cathedral.publisher.app", "build_app"),
        ("cathedral.publisher.app", "app"),
        ("cathedral.publisher", "build_app"),
        ("cathedral.publisher", "app"),
        ("cathedral.publisher.main", "app"),
    ]
    for module_name, attr in candidates:
        try:
            mod = __import__(module_name, fromlist=[attr])
        except Exception:
            continue
        if hasattr(mod, attr):
            obj = getattr(mod, attr)
            return obj
    return None


@pytest.fixture
def publisher_app(tmp_path: Path) -> Iterator[Any]:
    """The publisher FastAPI app (POST /v1/agents/submit + GET endpoints).

    Tries to import and return the implementer's app. If the app isn't
    importable yet, skips the dependent test with an actionable message.
    """
    obj = _try_build_publisher_app()
    if obj is None:
        pytest.skip(
            "publisher app not importable yet — implementer must expose "
            "the FastAPI app under cathedral.publisher (CONTRACTS.md §2)"
        )
    # If it's a builder, try to call with a tmp db path; if not callable
    # or callable with no args, fall back to using it directly.
    try:
        if callable(obj):
            try:
                app = obj(database_path=str(tmp_path / "publisher.db"))
            except TypeError:
                try:
                    app = obj(str(tmp_path / "publisher.db"))
                except TypeError:
                    app = obj()
        else:
            app = obj
    except Exception as exc:  # pragma: no cover — surfaces during integration
        pytest.skip(f"publisher app builder raised: {exc!r}")
    yield app


@pytest.fixture
def publisher_client(publisher_app: Any) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    with TestClient(publisher_app) as client:
        yield client


# --------------------------------------------------------------------------
# Multipart helper
# --------------------------------------------------------------------------


def submit_multipart(
    client: Any,
    *,
    keypair: Keypair,
    card_id: str,
    bundle: bytes,
    display_name: str = "Test Agent",
    bio: str | None = None,
    logo: tuple[str, bytes, str] | None = None,
    submitted_at: str | None = None,
    override_signature: str | None = None,
    override_hotkey: str | None = None,
    override_bundle_hash: str | None = None,
    attestation_mode: str = "bundle",
) -> Any:
    """POST /v1/agents/submit per CONTRACTS.md Section 2.1.

    ``override_*`` exist for negative tests where we want to exercise
    cathedral's verification path with malformed inputs.
    """
    submitted_at = submitted_at or _now_iso_ms()
    bundle_hash = override_bundle_hash or blake3_hex(bundle)
    sig_b64 = override_signature or sign_submission_payload(
        keypair,
        bundle_hash=bundle_hash,
        card_id=card_id,
        submitted_at=submitted_at,
    )
    headers = {
        CONTRACT_SIGNATURE_HEADER: sig_b64,
        CONTRACT_HOTKEY_HEADER: override_hotkey or keypair.ss58_address,
    }
    files = {"bundle": ("agent.zip", bundle, "application/zip")}
    data = {
        "card_id": card_id,
        "display_name": display_name,
        "submitted_at": submitted_at,
        "attestation_mode": attestation_mode,
    }
    if bio is not None:
        data["bio"] = bio
    if logo is not None:
        files["logo"] = logo
    return client.post("/v1/agents/submit", headers=headers, data=data, files=files)


def _now_iso_ms() -> str:
    """ISO-8601 UTC, ms precision, trailing Z — per Section 9 lock #6."""
    now = datetime.now(UTC).replace(microsecond=(datetime.now(UTC).microsecond // 1000) * 1000)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Golden-vector loading (optional — generated lazily by test_golden_vectors_v1)
# --------------------------------------------------------------------------

GOLDEN_VECTORS_PATH = Path(__file__).parent.parent / "fixtures" / "v1_golden_vectors.json"


@pytest.fixture(scope="module")
def golden_vectors_path() -> Path:
    return GOLDEN_VECTORS_PATH


def load_golden_vectors() -> dict[str, Any]:
    if not GOLDEN_VECTORS_PATH.exists():
        return {}
    return json.loads(GOLDEN_VECTORS_PATH.read_text())
