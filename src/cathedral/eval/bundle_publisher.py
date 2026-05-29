"""Eval-artifact bundle publisher (cathedralai/cathedral#75, PR 3).

Takes the ``TraceBundle`` produced by ``SshHermesRunner.run()``
(PR 2 — a tar.gz of Hermes state.db slice + session JSONs +
request_dumps + memories + skills + logs, plus a manifest dict),
encrypts the bundle bytes, uploads to Hippius under a separate
``eval-artifacts/`` prefix, signs the manifest with Cathedral's
Ed25519 eval signing key, uploads the signed manifest as a sibling
JSON object, and returns a ``PublishedArtifact`` carrying:

- the canonical ``manifest_hash`` (blake3 of canonical-JSON manifest)
  — this is the value PR 4 will sign as
  ``eval_artifact_manifest_hash`` in the v2 signed payload
- the ``bundle_url`` and ``manifest_url`` (s3:// URIs) — these go in
  PR 4's UNSIGNED response envelope per the Q4 contract; the
  validator verifies bundle integrity by re-hashing on download,
  not by trusting URL identity

Trust model:

- Hippius CIDs / S3 URIs can rotate. We don't sign URLs.
- The bundle bytes are addressable by their blake3 hash, embedded
  inside the manifest. The manifest's hash is what gets signed.
- A validator that downloads the bundle can re-hash it and verify
  the bytes match the manifest's ``bundle_blake3``. If they don't,
  the artifact is treated as tampered regardless of URL provenance.

Scope guardrails:

- Does NOT modify ``eval_runs`` schema (PR 4 territory).
- Does NOT plumb URLs into any wire response yet (PR 4 territory).
- Does NOT change the signed payload (PR 4 + validator-compat).
- Returns artifacts the orchestrator/scoring_pipeline will consume
  in PR 4. Until then this publisher is invoked but its output goes
  into a sidecar table-or-file (see ``persist_artifact_record``).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import blake3
import structlog

from cathedral.eval.eval_signer import EvalSigner
from cathedral.eval.ssh_hermes_runner import TraceBundle
from cathedral.storage import HippiusClient
from cathedral.storage.crypto import encrypt_bundle

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class BundlePublishError(Exception):
    """Any failure publishing a TraceBundle to Hippius."""


# --------------------------------------------------------------------------
# Output shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishedArtifact:
    """The result of publishing one TraceBundle.

    Fields:

    - ``eval_id`` / ``submission_id`` / ``cathedral_eval_round`` —
      identity, copied through from the input bundle.

    - ``manifest_hash`` — blake3 hex of the canonical-JSON of the
      *signed* manifest body (which itself contains the
      ``bundle_blake3`` over the encrypted bundle bytes). This is the
      value PR 4 will put in ``_SIGNED_KEYS_BY_VERSION[2]`` as
      ``eval_artifact_manifest_hash``. Any tampering with the
      manifest contents — file list, sha256s, proof_of_loop fields,
      or the bundle hash inside it — flips this value.

    - ``manifest_url`` — Hippius/S3 URI for the signed manifest JSON
      (a small object, KB-scale). Goes in the UNSIGNED envelope.

    - ``bundle_url`` — Hippius/S3 URI for the encrypted bundle bytes
      (MB-scale tar.gz, encrypted). Goes in the UNSIGNED envelope.

    - ``manifest_signature`` — base64-encoded Ed25519 signature over
      ``canonical_json(manifest_body)``. Validators verify against
      Cathedral's pinned public key. Bundled here for completeness;
      PR 4's signed payload references ``manifest_hash`` rather than
      the signature directly.

    - ``manifest_body`` — the in-memory canonical manifest dict, also
      stored inside the uploaded manifest object. Returned for
      callers that need to inspect the manifest without a round trip
      to Hippius.
    """

    eval_id: str
    submission_id: str
    cathedral_eval_round: str
    manifest_hash: str
    manifest_url: str
    bundle_url: str
    manifest_signature: str
    manifest_body: dict[str, Any]


# --------------------------------------------------------------------------
# Canonical-JSON over the manifest body
# --------------------------------------------------------------------------


def canonical_manifest_bytes(manifest_body: dict[str, Any]) -> bytes:
    """Serialize the manifest to canonical bytes for signing + hashing.

    Same rule the rest of Cathedral uses for signed payloads
    (sort_keys=True, separators=(',', ':'), default=str). We don't
    import cathedral.v1_types.canonical_json here only because we
    want this module loadable without the publisher import chain.
    """
    return json.dumps(manifest_body, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )


def blake3_hex(data: bytes) -> str:
    return blake3.blake3(data).hexdigest()


# --------------------------------------------------------------------------
# Publisher
# --------------------------------------------------------------------------


class EvalArtifactPublisher:
    """Encrypts, uploads, signs. Stateless across calls (each invocation
    handles one TraceBundle). Construct once, reuse per-eval.

    The ``HippiusClient`` instance and the ``EvalSigner`` are passed in
    so this module doesn't reach into publisher-app globals — keeps
    the import tree clean and the unit tests trivially mockable.
    """

    def __init__(
        self,
        *,
        hippius: HippiusClient,
        signer: EvalSigner,
    ) -> None:
        self.hippius = hippius
        self.signer = signer

    async def publish(self, bundle: TraceBundle) -> PublishedArtifact:
        """Upload the bundle + signed manifest, return the artifact handle.

        Order of operations matters for the trust chain:

        1. Read + encrypt the local tar.gz produced by PR 2.
        2. Upload the ciphertext to Hippius under ``eval-artifacts/``.
        3. Build the manifest body — copies PR 2's manifest dict and
           appends the upload-time fields (storage_uri, encryption
           key id, published_at).
        4. Sign canonical_json(manifest_body) with Cathedral's eval
           signing key. The signature is stored INSIDE the uploaded
           manifest object (alongside ``manifest_body``) so any future
           reader can verify offline.
        5. Compute ``manifest_hash = blake3(canonical_json(manifest_body))``
           — this is what PR 4's signed payload will reference.
        6. Upload the signed manifest JSON to Hippius under the same
           prefix.

        If step 1 or 2 fails, no manifest is uploaded — the caller
        gets a BundlePublishError and can retry. If step 6 fails after
        2 succeeded, the encrypted bundle is orphaned in Hippius (no
        manifest pointer); PR 5's cadence loop can re-derive a new
        manifest from the same bundle bytes on retry, so this isn't
        a permanent data loss.
        """
        local_path = bundle.bundle_tar_path
        if not local_path.exists():
            raise BundlePublishError(f"bundle tar missing on disk: {local_path}")

        # 1. Read + encrypt
        try:
            plaintext = await asyncio.to_thread(local_path.read_bytes)
        except OSError as e:
            raise BundlePublishError(f"read bundle tar: {e}") from e
        try:
            enc = encrypt_bundle(plaintext)
        except Exception as e:  # storage.crypto.EncryptionError is broad
            raise BundlePublishError(f"encrypt bundle: {e}") from e

        # 2. Upload ciphertext
        try:
            bundle_url = await self._put_eval_artifact(
                eval_id=bundle.eval_id,
                ciphertext=enc.ciphertext,
                bundle_hash_hex=bundle.bundle_blake3,
            )
        except Exception as e:
            raise BundlePublishError(f"upload bundle: {e}") from e

        # 3. Build the canonical manifest body. The PR 2 manifest
        # dict is treated as input-only; we copy it and add the
        # publisher's upload-time anchor fields.
        manifest_body: dict[str, Any] = dict(bundle.manifest)
        manifest_body.update(
            {
                "storage_uri": bundle_url,
                "encryption_key_id": enc.encryption_key_id,
                "published_at": datetime.now(UTC).isoformat(),
                "cathedral_publisher_version": "v1.1.0",
            }
        )

        # 4. Sign the canonical bytes
        canonical = canonical_manifest_bytes(manifest_body)
        try:
            # EvalSigner.sign takes a dict and canonicalizes internally,
            # so the over-the-wire bytes here are identical to what the
            # validator will canonicalize on read.
            manifest_signature = self.signer.sign(manifest_body)
        except Exception as e:
            raise BundlePublishError(f"sign manifest: {e}") from e

        # 5. Hash AFTER signing so the hash captures the body the sig
        # is over (the sig is a sidecar in the uploaded JSON envelope).
        manifest_hash = blake3_hex(canonical)

        # 6. Upload the manifest object — JSON envelope with both
        # the manifest body and its signature, so future readers can
        # verify offline without re-deriving canonical bytes.
        envelope = {
            "manifest_body": manifest_body,
            "manifest_signature": manifest_signature,
            "manifest_hash": manifest_hash,
            "signature_algorithm": "ed25519",
            "canonicalization": "json.sort_keys+compact",
        }
        try:
            manifest_url = await self._put_manifest(
                eval_id=bundle.eval_id,
                envelope_bytes=json.dumps(envelope).encode("utf-8"),
            )
        except Exception as e:
            raise BundlePublishError(f"upload manifest: {e}") from e

        logger.info(
            "eval_artifact_published",
            eval_id=bundle.eval_id,
            submission_id=bundle.submission_id,
            manifest_hash=manifest_hash,
            bundle_url=bundle_url,
            manifest_url=manifest_url,
            bundle_size_bytes=len(plaintext),
            ciphertext_size_bytes=len(enc.ciphertext),
        )

        return PublishedArtifact(
            eval_id=bundle.eval_id,
            submission_id=bundle.submission_id,
            cathedral_eval_round=bundle.cathedral_eval_round,
            manifest_hash=manifest_hash,
            manifest_url=manifest_url,
            bundle_url=bundle_url,
            manifest_signature=manifest_signature,
            manifest_body=manifest_body,
        )

    # ------------------------------------------------------------------
    # Hippius adapters
    #
    # We don't extend HippiusClient with new methods because that's a
    # shared module used by the submit path; the eval-artifact path
    # has a different key prefix and metadata shape. Keep it local.
    # ------------------------------------------------------------------

    async def _put_eval_artifact(
        self,
        *,
        eval_id: str,
        ciphertext: bytes,
        bundle_hash_hex: str,
    ) -> str:
        client = self.hippius._ensure_client()
        bucket = self.hippius.config.bucket
        env_prefix = self.hippius.config.env_prefix
        key = f"{env_prefix}eval-artifacts/{eval_id}.tar.gz.enc"

        def _put() -> str:
            try:
                client.put_object(  # type: ignore[attr-defined]
                    Bucket=bucket,
                    Key=key,
                    Body=ciphertext,
                    Metadata={
                        "bundle-hash": bundle_hash_hex,
                        "encryption": "aes-256-gcm",
                        "cathedral-version": "v1.1.0",
                        "artifact-kind": "eval-trace-bundle",
                    },
                    ContentType="application/octet-stream",
                )
            except Exception as e:
                raise BundlePublishError(f"hippius put eval bundle: {e}") from e
            return f"s3://{bucket}/{key}"

        return await asyncio.to_thread(_put)

    async def _put_manifest(
        self,
        *,
        eval_id: str,
        envelope_bytes: bytes,
    ) -> str:
        client = self.hippius._ensure_client()
        bucket = self.hippius.config.bucket
        env_prefix = self.hippius.config.env_prefix
        key = f"{env_prefix}eval-artifacts/{eval_id}.manifest.json"

        def _put() -> str:
            try:
                client.put_object(  # type: ignore[attr-defined]
                    Bucket=bucket,
                    Key=key,
                    Body=envelope_bytes,
                    Metadata={
                        "cathedral-version": "v1.1.0",
                        "artifact-kind": "eval-trace-manifest",
                    },
                    ContentType="application/json",
                )
            except Exception as e:
                raise BundlePublishError(f"hippius put manifest: {e}") from e
            return f"s3://{bucket}/{key}"

        return await asyncio.to_thread(_put)


__all__ = [
    "BundlePublishError",
    "EvalArtifactPublisher",
    "PublishedArtifact",
    "blake3_hex",
    "canonical_manifest_bytes",
]
