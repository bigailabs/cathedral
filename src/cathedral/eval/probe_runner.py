"""ProbeRunner - publisher-side client for a miner-owned long-lived probe.

Stage B.4 of BUILD_V1.md. The probe runtime (B.1) accepts POST /probe/run
on a Polaris-rented CPU box the miner controls. The probe signs each
ProbeOutput with the miner's hotkey (sr25519) before returning.

This runner POSTs the bundle bytes + eval task to the probe, verifies the
sr25519 signature against the miner's ss58, and returns a PolarisRunResult
in the same shape as the in-process runners so the orchestrator does not
need to branch.

Wired by `CATHEDRAL_EVAL_MODE=probe`. The `probe_url` and the miner ss58
travel on the submission row (publisher-side schema work is downstream).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import blake3
import httpx
import structlog

from cathedral.eval.runner_types import (
    PolarisRunnerError,
    PolarisRunResult,
)
from cathedral.v1_types import EvalTask

logger = structlog.get_logger(__name__)


class ProbeSignatureError(PolarisRunnerError):
    """ProbeOutput sr25519 signature did not verify against the miner ss58."""


class ProbeTransportError(PolarisRunnerError):
    """Network / HTTP error talking to the probe box."""


@dataclass(frozen=True)
class ProbeAttestation:
    """The signed envelope returned by the probe."""

    miner_ss58: str
    miner_signature_b64: str
    output_card_hash: str
    task_hash: str

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "miner_ss58": self.miner_ss58,
            "miner_signature": self.miner_signature_b64,
            "output_card_hash": self.output_card_hash,
            "task_hash": self.task_hash,
        }


def _canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _verify_sr25519(*, public_ss58: str, message: bytes, signature_b64: str) -> None:
    """Verify an sr25519 signature against the given ss58 address.

    Uses `bittensor_wallet.Keypair` (already pulled in via the bittensor dep).
    Raises ProbeSignatureError on any failure.
    """
    try:
        from bittensor_wallet import Keypair
    except ImportError as e:
        raise ProbeSignatureError(
            "bittensor_wallet not installed; cannot verify miner signature"
        ) from e

    try:
        sig = base64.b64decode(signature_b64)
    except (ValueError, TypeError) as e:
        raise ProbeSignatureError(f"signature base64 invalid: {e}") from e

    try:
        kp = Keypair(ss58_address=public_ss58)
        if not kp.verify(message, sig):
            raise ProbeSignatureError("miner sr25519 signature did not verify")
    except ProbeSignatureError:
        raise
    except Exception as e:
        raise ProbeSignatureError(f"sr25519 verify error: {e}") from e


class ProbeRunner:
    """POST /probe/run -> ProbeOutput -> verify -> PolarisRunResult.

    Init args carry per-miner config; `run()` follows the PolarisRunner
    Protocol so the orchestrator can wire this exactly like any other
    PolarisRunner.

    Parameters
    ----------
    probe_url:
        Externally reachable HTTPS URL for the miner's probe (e.g.
        `https://probe-1.cathedral.computer:8088`).
    miner_ss58:
        Expected miner ss58 (the public key the probe is supposed to sign
        with). Must match the hotkey that was registered for the
        submission - verifying against an arbitrary key would defeat the
        purpose of the signature.
    request_timeout_secs:
        Per-request timeout. Probes can take a while when the model is
        slow, but we cap to avoid hanging the orchestrator.
    """

    def __init__(
        self,
        *,
        probe_url: str,
        miner_ss58: str,
        request_timeout_secs: float = 120.0,
    ) -> None:
        self._probe_url = probe_url.rstrip("/")
        self._miner_ss58 = miner_ss58
        self._timeout = request_timeout_secs

    async def run(
        self,
        *,
        bundle_bytes: bytes,
        bundle_hash: str,
        task: EvalTask,
        miner_hotkey: str,
        submission: dict[str, Any] | None = None,
    ) -> PolarisRunResult:
        if miner_hotkey != self._miner_ss58:
            raise ProbeSignatureError(
                f"miner_hotkey {miner_hotkey} does not match runner's miner_ss58 {self._miner_ss58}"
            )

        sub_id = (submission or {}).get("id") or f"sub_{uuid4().hex[:12]}"
        body = {
            "submission_id": str(sub_id),
            "card_id": task.card_id,
            "prompt": task.prompt,
            "bundle_bytes_b64": base64.b64encode(bundle_bytes).decode(),
            "bundle_hash": bundle_hash,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(f"{self._probe_url}/probe/run", json=body)
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPError as e:
            raise ProbeTransportError(f"probe transport error: {e}") from e

        return self._to_result(payload, task=task, sub_id=str(sub_id))

    def _to_result(
        self, payload: dict[str, Any], *, task: EvalTask, sub_id: str
    ) -> PolarisRunResult:
        required = (
            "submission_id",
            "card_id",
            "output_card",
            "output_card_hash",
            "task_hash",
            "ran_at",
            "miner_signature",
        )
        for k in required:
            if k not in payload:
                raise PolarisRunnerError(f"probe response missing field: {k}")

        if payload["card_id"] != task.card_id:
            raise PolarisRunnerError(
                f"probe returned card_id {payload['card_id']!r}, expected {task.card_id!r}"
            )

        output_card = payload["output_card"]
        if not isinstance(output_card, dict):
            raise PolarisRunnerError("probe output_card is not a JSON object")

        computed_hash = blake3.blake3(_canonical_json(output_card)).hexdigest()
        if computed_hash != payload["output_card_hash"]:
            raise PolarisRunnerError(
                f"output_card_hash mismatch: probe={payload['output_card_hash']} "
                f"computed={computed_hash}"
            )

        signed_payload = {k: v for k, v in payload.items() if k != "miner_signature"}
        _verify_sr25519(
            public_ss58=self._miner_ss58,
            message=_canonical_json(signed_payload),
            signature_b64=payload["miner_signature"],
        )

        attestation = ProbeAttestation(
            miner_ss58=self._miner_ss58,
            miner_signature_b64=payload["miner_signature"],
            output_card_hash=payload["output_card_hash"],
            task_hash=payload["task_hash"],
        )

        return PolarisRunResult(
            polaris_agent_id=f"probe_{sub_id}",
            polaris_run_id=f"prun_{uuid4().hex[:12]}",
            output_card_json=output_card,
            duration_ms=int(payload.get("duration_ms", 0)),
            errors=[],
            attestation=None,
            probe_attestation=attestation.to_storage_dict(),
        )


__all__ = [
    "ProbeAttestation",
    "ProbeRunner",
    "ProbeSignatureError",
    "ProbeTransportError",
]
