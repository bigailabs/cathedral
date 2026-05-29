# ruff: noqa: N803
# N803 (lowercase argument names): boto3's put_object uses PascalCase
# kwargs (Bucket, Key, Body, Metadata, ContentType). Our stub mirrors
# that exactly so the mock is a drop-in for the real client.
"""Unit tests for EvalArtifactPublisher (cathedralai/cathedral#75 PR 3).

Mocks Hippius S3 client at the boundary; uses a real Ed25519 keypair
for signing so the manifest signature verifies end-to-end. Covers:

- happy path: read bundle, encrypt, upload bundle, build manifest,
  sign, hash, upload manifest, return PublishedArtifact
- canonical_manifest_bytes + blake3_hex helpers (deterministic across
  Python dict order)
- manifest signature verifies with the same key
- manifest_hash changes when any signed field changes
- bundle upload failure -> BundlePublishError (no orphan manifest)
- manifest upload failure -> BundlePublishError (orphan bundle is OK)
"""

from __future__ import annotations

import base64
import importlib.util as _ilu
import json
import os
import secrets
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import blake3
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# Same direct-module-load dance as test_ssh_hermes_runner.py. We
# pre-register a stub `cathedral.eval` package module so the chain
# `from cathedral.eval.X import Y` doesn't trigger __init__.py
# (which would re-enter the publisher import cycle).
_ROOT = Path(__file__).resolve().parents[2]

if "cathedral.eval" not in sys.modules:
    _stub_eval_pkg = type(sys)("cathedral.eval")
    _stub_eval_pkg.__path__ = [str(_ROOT / "src" / "cathedral" / "eval")]  # type: ignore[attr-defined]
    sys.modules["cathedral.eval"] = _stub_eval_pkg

# ssh_hermes_runner needs to be loadable so TraceBundle is in scope.
# Register under BOTH the test-local name AND the canonical name so
# `from cathedral.eval.ssh_hermes_runner import TraceBundle` resolves
# without triggering cathedral.eval.__init__.
if "cathedral.eval.ssh_hermes_runner" in sys.modules:
    _shr = sys.modules["cathedral.eval.ssh_hermes_runner"]
else:
    _SHR_PATH = _ROOT / "src" / "cathedral" / "eval" / "ssh_hermes_runner.py"
    _spec = _ilu.spec_from_file_location("cathedral.eval.ssh_hermes_runner", _SHR_PATH)
    assert _spec and _spec.loader
    _shr = _ilu.module_from_spec(_spec)
    sys.modules["cathedral.eval.ssh_hermes_runner"] = _shr
    sys.modules["_ssh_hermes_runner_for_test"] = _shr
    _spec.loader.exec_module(_shr)
TraceBundle = _shr.TraceBundle


# --------------------------------------------------------------------------
# Set CATHEDRAL_KEK_HEX before importing bundle_publisher (which imports
# storage.crypto, which lazy-reads the env var at first use).
# --------------------------------------------------------------------------

os.environ.setdefault("CATHEDRAL_KEK_HEX", secrets.token_bytes(32).hex())

# bundle_publisher imports scoring_pipeline -> publisher.repository ->
# publisher.app. That chain is fragile; we mirror the ssh_hermes_runner
# pattern by direct-loading.
_BP_PATH = _ROOT / "src" / "cathedral" / "eval" / "bundle_publisher.py"

# But bundle_publisher imports EvalSigner from scoring_pipeline, which
# would re-enter the publisher import. To dodge that, we direct-load
# scoring_pipeline.EvalSigner only (skipping its module's other imports
# isn't worth the hack). The simpler route is: register a stub
# scoring_pipeline module that exposes just EvalSigner. We import the
# real Ed25519 key and reimplement the .sign() contract inline.

import json as _json  # noqa: E402


class _MinimalEvalSigner:
    """Minimal stand-in for cathedral.eval.scoring_pipeline.EvalSigner.

    Same .sign(dict) -> base64 signature contract. Validated against
    the real EvalSigner's docstring contract; we don't import the real
    class here because doing so triggers a publisher import cycle.
    """

    def __init__(self, sk: Ed25519PrivateKey) -> None:
        self._sk = sk

    def sign(self, eval_run_dict: dict[str, Any]) -> str:
        payload = _json.dumps(
            eval_run_dict, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
        return base64.b64encode(self._sk.sign(payload)).decode("ascii")


# Register a stub `cathedral.eval.scoring_pipeline` so bundle_publisher's
# `from cathedral.eval.scoring_pipeline import EvalSigner` resolves.
# CRITICAL: if the real scoring_pipeline has already been imported (e.g.
# by another test that loaded the publisher app first), reuse the real
# one. Otherwise we register our stub. On test session teardown, restore
# the original sys.modules state so a later test that needs the real
# publisher can load it fresh.
_PRIOR_SCORING = sys.modules.get("cathedral.eval.scoring_pipeline")
_INSTALLED_STUB = False
if _PRIOR_SCORING is None:
    _stub_scoring = type(sys)("cathedral.eval.scoring_pipeline")
    _stub_scoring.EvalSigner = _MinimalEvalSigner
    sys.modules["cathedral.eval.scoring_pipeline"] = _stub_scoring
    _INSTALLED_STUB = True

# Now direct-load bundle_publisher under the canonical name so other
# tests importing it later get the cached module rather than re-execing.
_bp_spec = _ilu.spec_from_file_location("cathedral.eval.bundle_publisher", _BP_PATH)
assert _bp_spec and _bp_spec.loader
_bp = _ilu.module_from_spec(_bp_spec)
sys.modules["cathedral.eval.bundle_publisher"] = _bp
sys.modules["_bundle_publisher_for_test"] = _bp
_bp_spec.loader.exec_module(_bp)

# If we installed a stub, undo it now so subsequent test modules that
# need the real cathedral.eval.scoring_pipeline can import it freshly.
# bundle_publisher has already captured its own EvalSigner reference
# from the stub; the publish() path is the one that uses it, and
# tests pass `signer=_MinimalEvalSigner(...)` directly so the stub's
# EvalSigner isn't actually used post-import.
if _INSTALLED_STUB:
    del sys.modules["cathedral.eval.scoring_pipeline"]

EvalArtifactPublisher = _bp.EvalArtifactPublisher
BundlePublishError = _bp.BundlePublishError
PublishedArtifact = _bp.PublishedArtifact
canonical_manifest_bytes = _bp.canonical_manifest_bytes
blake3_hex_helper = _bp.blake3_hex


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def ed25519_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    return sk, pk


@pytest.fixture
def signer(ed25519_keypair) -> _MinimalEvalSigner:
    sk, _ = ed25519_keypair
    return _MinimalEvalSigner(sk)


@pytest.fixture
def fake_hippius() -> Any:
    """Stub HippiusClient: in-memory put_object capture."""
    storage: dict[str, dict[str, Any]] = {}

    s3_client = MagicMock()

    def _put_object(*, Bucket: str, Key: str, Body: bytes, Metadata: dict, ContentType: str):
        storage[f"{Bucket}/{Key}"] = {
            "Body": Body,
            "Metadata": Metadata,
            "ContentType": ContentType,
        }
        return {"ETag": "fake-etag"}

    s3_client.put_object = MagicMock(side_effect=_put_object)

    client = MagicMock()
    client._ensure_client = MagicMock(return_value=s3_client)
    client.config.bucket = "cathedral-test"
    client.config.env_prefix = "test/"
    client._storage = storage  # expose for inspection
    return client


@pytest.fixture
def bundle_with_tar(tmp_path: Path) -> Any:
    """A real tar.gz on disk + a TraceBundle pointing at it."""
    workdir = tmp_path / "bundle_src"
    workdir.mkdir()
    (workdir / "state.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    (workdir / "session.json").write_text(json.dumps({"session_id": "s1", "messages": []}))

    tar_path = tmp_path / "cathedral-eval-test.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(workdir, arcname=".")

    bundle_hash = blake3.blake3(tar_path.read_bytes()).hexdigest()

    manifest = {
        "manifest_version": 1,
        "eval_id": "eval-test-001",
        "submission_id": "sub-test-001",
        "cathedral_eval_round": "eu-ai-act-1-0-abcd1234",
        "captured_at": datetime.now(UTC).isoformat(),
        "hermes_version": "hermes 0.13.0",
        "files": [
            {
                "path": "state.db",
                "sha256": "abc" * 21 + "a",
                "byte_length": 116,
                "content_type": "application/vnd.sqlite3",
            },
        ],
        "proof_of_loop": {
            "session_id": "s1",
            "tool_call_count": 0,
            "api_call_count": 0,
            "request_dump_file_count": 0,
            "system_prompt_includes_soul_md": False,
            "system_prompt_includes_agents_md": False,
            "system_prompt_includes_memory_md": False,
            "tool_calls_observed": [],
        },
        "bundle_blake3": bundle_hash,
    }

    return TraceBundle(
        eval_id="eval-test-001",
        submission_id="sub-test-001",
        cathedral_eval_round="eu-ai-act-1-0-abcd1234",
        bundle_tar_path=tar_path,
        manifest=manifest,
        bundle_blake3=bundle_hash,
    )


# --------------------------------------------------------------------------
# canonical_manifest_bytes
# --------------------------------------------------------------------------


def test_canonical_manifest_bytes_is_deterministic():
    a = {"x": 1, "y": [1, 2, 3], "z": {"b": 2, "a": 1}}
    b = {"y": [1, 2, 3], "z": {"a": 1, "b": 2}, "x": 1}
    assert canonical_manifest_bytes(a) == canonical_manifest_bytes(b)


def test_canonical_manifest_bytes_compact_no_whitespace():
    out = canonical_manifest_bytes({"a": 1, "b": 2})
    assert b" " not in out
    assert out == b'{"a":1,"b":2}'


def test_blake3_helper_matches_library():
    out = blake3_hex_helper(b"hello cathedral")
    assert out == blake3.blake3(b"hello cathedral").hexdigest()
    assert len(out) == 64


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_happy_path_returns_published_artifact(fake_hippius, signer, bundle_with_tar):
    publisher = EvalArtifactPublisher(hippius=fake_hippius, signer=signer)
    result = await publisher.publish(bundle_with_tar)

    assert isinstance(result, PublishedArtifact)
    assert result.eval_id == "eval-test-001"
    assert result.submission_id == "sub-test-001"
    assert result.cathedral_eval_round == "eu-ai-act-1-0-abcd1234"

    # URLs are s3 with the expected key shape
    assert result.bundle_url.startswith("s3://cathedral-test/test/eval-artifacts/")
    assert result.bundle_url.endswith("eval-test-001.tar.gz.enc")
    assert result.manifest_url.startswith("s3://cathedral-test/test/eval-artifacts/")
    assert result.manifest_url.endswith("eval-test-001.manifest.json")

    # Manifest hash is 64-hex blake3 of canonical manifest
    assert len(result.manifest_hash) == 64
    assert all(c in "0123456789abcdef" for c in result.manifest_hash)
    assert (
        result.manifest_hash
        == blake3.blake3(canonical_manifest_bytes(result.manifest_body)).hexdigest()
    )

    # Manifest signature is a valid b64-decodable Ed25519 signature
    sig_bytes = base64.b64decode(result.manifest_signature)
    assert len(sig_bytes) == 64  # Ed25519 signature length


@pytest.mark.asyncio
async def test_publish_uploads_both_bundle_and_manifest_to_hippius(
    fake_hippius, signer, bundle_with_tar
):
    publisher = EvalArtifactPublisher(hippius=fake_hippius, signer=signer)
    await publisher.publish(bundle_with_tar)

    storage = fake_hippius._storage
    # Two keys: encrypted bundle + manifest envelope
    assert len(storage) == 2

    bundle_key = "cathedral-test/test/eval-artifacts/eval-test-001.tar.gz.enc"
    manifest_key = "cathedral-test/test/eval-artifacts/eval-test-001.manifest.json"
    assert bundle_key in storage
    assert manifest_key in storage

    # Bundle has the right metadata
    bundle_obj = storage[bundle_key]
    assert bundle_obj["Metadata"]["encryption"] == "aes-256-gcm"
    assert bundle_obj["Metadata"]["artifact-kind"] == "eval-trace-bundle"
    assert bundle_obj["ContentType"] == "application/octet-stream"
    # Ciphertext is non-trivial (encryption adds GCM tag)
    assert len(bundle_obj["Body"]) > 0

    # Manifest envelope is JSON and parses back
    manifest_obj = storage[manifest_key]
    assert manifest_obj["ContentType"] == "application/json"
    envelope = json.loads(manifest_obj["Body"])
    assert "manifest_body" in envelope
    assert "manifest_signature" in envelope
    assert "manifest_hash" in envelope
    assert envelope["signature_algorithm"] == "ed25519"


@pytest.mark.asyncio
async def test_manifest_signature_verifies_with_publisher_key(
    fake_hippius, signer, bundle_with_tar, ed25519_keypair
):
    """End-to-end: sign with publisher's key, verify with the matching
    public key. Tests the contract a validator will follow on read."""
    _, pk = ed25519_keypair
    publisher = EvalArtifactPublisher(hippius=fake_hippius, signer=signer)
    result = await publisher.publish(bundle_with_tar)

    canonical = canonical_manifest_bytes(result.manifest_body)
    sig = base64.b64decode(result.manifest_signature)
    # Will raise InvalidSignature if it doesn't verify
    pk.verify(sig, canonical)


@pytest.mark.asyncio
async def test_manifest_hash_flips_when_any_signed_field_changes(
    fake_hippius, signer, bundle_with_tar
):
    """Any byte-level change to the manifest body flips the hash.
    This is what PR 4's signed_payload[eval_artifact_manifest_hash]
    is protecting."""
    publisher = EvalArtifactPublisher(hippius=fake_hippius, signer=signer)
    r1 = await publisher.publish(bundle_with_tar)

    # Mutate a load-bearing field on the manifest BODY (not the bundle).
    # In production a tamperer would also need to upload a matching
    # bundle; here we just check the hash detects the body change.
    tampered_body = dict(r1.manifest_body)
    tampered_body["proof_of_loop"] = {**tampered_body["proof_of_loop"], "tool_call_count": 999}
    tampered_canonical = canonical_manifest_bytes(tampered_body)
    tampered_hash = blake3.blake3(tampered_canonical).hexdigest()
    assert tampered_hash != r1.manifest_hash


# --------------------------------------------------------------------------
# Upload errors
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_raises_on_missing_tar(fake_hippius, signer, tmp_path: Path):
    bogus_bundle = TraceBundle(
        eval_id="x",
        submission_id="y",
        cathedral_eval_round="r",
        bundle_tar_path=tmp_path / "does-not-exist.tar.gz",
        manifest={"manifest_version": 1, "files": []},
        bundle_blake3="0" * 64,
    )
    publisher = EvalArtifactPublisher(hippius=fake_hippius, signer=signer)
    with pytest.raises(BundlePublishError) as exc:
        await publisher.publish(bogus_bundle)
    assert "missing on disk" in str(exc.value)


@pytest.mark.asyncio
async def test_publish_raises_when_bundle_upload_fails(signer, bundle_with_tar, monkeypatch):
    """When the Hippius bundle upload raises, the manifest is never
    uploaded — no orphan manifest pointing at a nonexistent bundle."""
    failing_client = MagicMock()
    s3 = MagicMock()
    s3.put_object = MagicMock(side_effect=RuntimeError("simulated S3 failure"))
    failing_client._ensure_client = MagicMock(return_value=s3)
    failing_client.config.bucket = "cathedral-test"
    failing_client.config.env_prefix = "test/"

    publisher = EvalArtifactPublisher(hippius=failing_client, signer=signer)
    with pytest.raises(BundlePublishError) as exc:
        await publisher.publish(bundle_with_tar)
    assert "upload bundle" in str(exc.value)
    # Only the bundle put_object was attempted; no second call for manifest
    assert s3.put_object.call_count == 1


@pytest.mark.asyncio
async def test_publish_raises_when_manifest_upload_fails(signer, bundle_with_tar, monkeypatch):
    """When manifest upload fails after a successful bundle upload, the
    bundle is orphaned in Hippius — that's tolerable; the docstring on
    publish() explicitly addresses this. PR 5's cadence loop can
    re-derive a manifest from the same bundle bytes."""
    call_count = {"n": 0}

    def _put(*, Bucket: str, Key: str, Body: bytes, Metadata: dict, ContentType: str):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"ETag": "ok"}  # bundle upload succeeds
        raise RuntimeError("simulated manifest upload failure")

    s3 = MagicMock()
    s3.put_object = MagicMock(side_effect=_put)
    client = MagicMock()
    client._ensure_client = MagicMock(return_value=s3)
    client.config.bucket = "cathedral-test"
    client.config.env_prefix = "test/"

    publisher = EvalArtifactPublisher(hippius=client, signer=signer)
    with pytest.raises(BundlePublishError) as exc:
        await publisher.publish(bundle_with_tar)
    assert "upload manifest" in str(exc.value)
    # Bundle upload attempted, then manifest upload attempted (failed)
    assert s3.put_object.call_count == 2


# --------------------------------------------------------------------------
# Manifest body shape
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manifest_body_carries_input_plus_upload_fields(
    fake_hippius, signer, bundle_with_tar
):
    publisher = EvalArtifactPublisher(hippius=fake_hippius, signer=signer)
    result = await publisher.publish(bundle_with_tar)

    body = result.manifest_body
    # Carry-over fields from the input bundle.manifest
    assert body["manifest_version"] == 1
    assert body["eval_id"] == "eval-test-001"
    assert body["hermes_version"] == "hermes 0.13.0"
    assert len(body["files"]) == 1

    # Publisher-added fields
    assert body["storage_uri"].startswith("s3://cathedral-test/")
    assert body["encryption_key_id"]  # opaque token
    assert body["published_at"]  # ISO timestamp
    assert body["cathedral_publisher_version"] == "v1.1.0"
