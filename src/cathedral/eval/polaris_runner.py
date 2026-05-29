"""Polaris API client — spawn Hermes container with bundle, capture card.

CONTRACTS.md Section 6 step 3: cathedral fetches the encrypted bundle
from Hippius, decrypts in-memory, writes to ephemeral container volume,
spawns Hermes via Polaris, captures stdout last line as Card JSON,
terminates.

The Polaris API contract is in `polariscomputer/polaris/api/routers/`.
For v1 the integration agent will wire to the real endpoint; this
module exposes a small `PolarisRunner` Protocol so the orchestrator can
be unit-tested with a `StubPolarisRunner` and integration-tested with
a `HttpPolarisRunner` once Polaris ships the container-runner endpoint.

When `CATHEDRAL_EVAL_MODE=stub`, the orchestrator wires `StubPolarisRunner`
which fabricates a card from the task — useful for end-to-end smoke
tests before the real Polaris endpoint exists.

When `CATHEDRAL_EVAL_MODE=bundle`, the orchestrator wires
`BundleCardRunner`, the BYO-compute path: the miner already ran their
agent, baked the resulting Card JSON into the bundle at
`artifacts/last-card.json`, and the publisher's job is just to read +
score it. No Polaris call, no fabrication.

When `CATHEDRAL_EVAL_MODE=polaris` (Tier A — Polaris-hosted miners),
the orchestrator wires `PolarisRuntimeRunner`. The miner's encrypted
bundle is referenced by a short-lived presigned URL handed to the
Polaris `runtime-evaluate` endpoint. Polaris deploys Cathedral's
runtime image against the miner's bundle, runs the eval task, and
returns a signed attestation `{payload, signature, public_key}` over
`(submission_id, task_id, task_hash, output_hash, deployment_id,
completed_at)`. The runner re-derives `task_hash` and `output_hash`,
checks the Ed25519 signature against `POLARIS_ATTESTATION_PUBLIC_KEY`,
and only then returns the Card. Any mismatch raises
`PolarisAttestationError` so the orchestrator records the run as a
runner failure and persists no score.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

import blake3
import httpx
import structlog
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from cathedral.v1_types import EvalTask

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------
# Base runner types now live in eval/runner_types.py (the kept module the
# live SAT prober depends on). They are re-exported here so every existing
# `from cathedral.eval.polaris_runner import ...` path keeps resolving while
# the card-runner machinery below is stripped. The card runners that remain
# in this module (BundleCardRunner, HttpPolarisRunner, PolarisRuntimeRunner,
# the failing/malformed stubs) still reference these names at runtime.
# --------------------------------------------------------------------------
from cathedral.eval.runner_types import (  # noqa: E402
    PolarisAttestation,
    PolarisAttestationError,
    PolarisRunner,
    PolarisRunnerError,
    PolarisRunResult,
    StubPolarisRunner,
    _juris_for,
)

# Re-export the base runner types so existing
# `from cathedral.eval.polaris_runner import ...` paths keep resolving while
# the card-runner machinery in this module is stripped. These names are pulled
# from runner_types above; listing them here marks them as intentional exports.
__all__ = [
    "PolarisAttestation",
    "PolarisAttestationError",
    "PolarisRunResult",
    "PolarisRunner",
    "PolarisRunnerError",
    "StubPolarisRunner",
    "_juris_for",
]


class FailingStubPolarisRunner:
    """Always raises `PolarisRunnerError` — drives the retry-exhaustion path."""

    async def run(
        self,
        *,
        bundle_bytes: bytes,
        bundle_hash: str,
        task: EvalTask,
        miner_hotkey: str,
        submission: dict[str, Any] | None = None,
    ) -> PolarisRunResult:
        del bundle_bytes, bundle_hash, task, miner_hotkey
        raise PolarisRunnerError("stub-fail-polaris: simulated transport failure")


class MalformedStubPolarisRunner:
    """Returns a card whose JSON is structurally invalid — preflight rejects."""

    async def run(
        self,
        *,
        bundle_bytes: bytes,
        bundle_hash: str,
        task: EvalTask,
        miner_hotkey: str,
        submission: dict[str, Any] | None = None,
    ) -> PolarisRunResult:
        del bundle_bytes, bundle_hash
        return PolarisRunResult(
            polaris_agent_id=f"agt_bad_{task.card_id}",
            polaris_run_id=f"run_bad_{uuid4().hex[:12]}",
            output_card_json={
                # Missing every required Card field — Card.model_validate
                # raises and the scoring pipeline records weighted_score=0
                # with errors=[...]. Keep `id` so insertion still works.
                "id": task.card_id,
                "_intentionally_malformed": True,
                "worker_owner_hotkey": miner_hotkey,
            },
            duration_ms=5,
            errors=["malformed card from stub-bad-card mode"],
        )


# --------------------------------------------------------------------------
# BYO-compute runner — score the miner's pre-baked card from the bundle
# --------------------------------------------------------------------------


# Bundle paths the miner is allowed to use for the pre-baked card. Order
# matters: the first match wins. `artifacts/last-card.json` is the
# canonical Hermes convention (matches the test miner script); `card.json`
# at root is accepted as a friendlier alias for hand-crafted bundles.
_BUNDLE_CARD_PATHS: tuple[str, ...] = (
    "artifacts/last-card.json",
    "card.json",
)


def _find_card_in_bundle(bundle_bytes: bytes) -> tuple[str, bytes] | None:
    """Locate the miner-supplied card JSON inside a zip bundle.

    Returns (member_name, raw_bytes) for the first match in
    `_BUNDLE_CARD_PATHS`, or None if the bundle has no recognised card
    file. Tolerates a single top-level directory prefix (matches the
    same convention as `bundle_extractor._find_first`).
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(bundle_bytes))
    except zipfile.BadZipFile:
        return None
    with zf:
        names = {info.filename.replace("\\", "/"): info for info in zf.infolist()}
        for candidate in _BUNDLE_CARD_PATHS:
            if candidate in names:
                return candidate, zf.read(names[candidate])
            # Allow a single-directory nesting prefix, e.g.
            # `my-agent/artifacts/last-card.json`.
            for member, info in names.items():
                if (
                    member.endswith("/" + candidate)
                    and member.count("/") == candidate.count("/") + 1
                ):
                    return member, zf.read(info)
    return None


class BundleCardRunner:
    """Path A — score the miner's pre-baked Card JSON instead of running an eval.

    Wired by `CATHEDRAL_EVAL_MODE=bundle`. The orchestrator already
    decrypted the bundle and handed us the plaintext bytes; we look for
    `artifacts/last-card.json` (or `card.json`), parse it as JSON, and
    return it as the runner's output. The downstream scoring pipeline
    runs preflight + scorer against this dict exactly as if it had come
    from a Polaris-spawned Hermes container — the only difference is
    that the LLM work happened on the miner's hardware.

    Failure modes are signalled via `PolarisRunnerError` so the
    orchestrator's existing retry path (CONTRACTS.md §6) handles them
    uniformly:

      - missing card file       -> PolarisRunnerError("bundle missing ...")
      - unreadable zip          -> PolarisRunnerError("bundle not a valid zip")
      - malformed JSON          -> PolarisRunnerError("card.json malformed: ...")
      - non-object JSON root    -> PolarisRunnerError("card.json must be a JSON object")

    After 3 such errors, the orchestrator marks the submission
    `rejected` with `rejection_reason="polaris exhausted retries"` and
    records a zero-score eval_run with the error list — same surface
    behaviour as a Polaris-side failure.
    """

    def __init__(self) -> None:
        # No I/O, no state — runner instances are cheap and stateless,
        # matching the StubPolarisRunner contract.
        pass

    async def run(
        self,
        *,
        bundle_bytes: bytes,
        bundle_hash: str,
        task: EvalTask,
        miner_hotkey: str,
        submission: dict[str, Any] | None = None,
    ) -> PolarisRunResult:
        del bundle_hash, miner_hotkey  # not needed; scoring_pipeline re-pins attribution

        start = datetime.now(UTC)
        located = _find_card_in_bundle(bundle_bytes)
        if located is None:
            raise PolarisRunnerError(
                f"bundle missing card file (looked for {', '.join(_BUNDLE_CARD_PATHS)})"
            )
        member, raw = located
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as e:
            raise PolarisRunnerError(f"{member} is not valid utf-8: {e}") from e
        except json.JSONDecodeError as e:
            raise PolarisRunnerError(f"{member} malformed: {e}") from e
        if not isinstance(decoded, dict):
            raise PolarisRunnerError(
                f"{member} must be a JSON object, got {type(decoded).__name__}"
            )

        # BYO-compute path: no polaris_agent_id, no polaris_run_id (Polaris
        # didn't run anything). scoring_pipeline.score_and_sign treats an
        # empty polaris_agent_id as "not Polaris-verified" and skips the
        # 1.10x runtime multiplier — that's the correct semantics here.
        duration_ms = int((datetime.now(UTC) - start).total_seconds() * 1000)
        return PolarisRunResult(
            polaris_agent_id="",
            polaris_run_id=f"bundle-{task.card_id}-e{task.epoch}r{task.round_index}",
            output_card_json=decoded,
            duration_ms=duration_ms,
            errors=[],
        )


# --------------------------------------------------------------------------
# Real Polaris HTTP runner
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpPolarisRunnerConfig:
    base_url: str
    api_token: str
    poll_interval_secs: float = 5.0
    deadline_buffer_secs: int = 5 * 60  # +5 min above task.deadline_minutes


class HttpPolarisRunner:
    """Production runner: deploy bundle, poll, fetch card, teardown.

    The exact Polaris endpoint shapes are pinned in the
    polariscomputer cathedral-contract worktree. This client expects:

      POST {base_url}/polaris/agents/cathedral-eval
        body: { bundle_b64, bundle_hash, task: EvalTask, miner_hotkey }
        -> { polaris_agent_id, polaris_run_id, status: "running" }

      GET  {base_url}/polaris/runs/{run_id}
        -> { status: "running"|"success"|"failed",
             output_card?: CardJSON, error?: str }

    If Polaris's endpoint contract changes, swap the body shape here and
    the rest of the pipeline stays unchanged.
    """

    def __init__(self, config: HttpPolarisRunnerConfig) -> None:
        self.config = config

    async def run(
        self,
        *,
        bundle_bytes: bytes,
        bundle_hash: str,
        task: EvalTask,
        miner_hotkey: str,
        submission: dict[str, Any] | None = None,
    ) -> PolarisRunResult:
        import base64

        body = {
            "bundle_b64": base64.b64encode(bundle_bytes).decode("ascii"),
            "bundle_hash": bundle_hash,
            "task": task.model_dump(mode="json"),
            "miner_hotkey": miner_hotkey,
        }
        deadline_secs = task.deadline_minutes * 60 + self.config.deadline_buffer_secs
        start = datetime.now(UTC)

        async with httpx.AsyncClient(
            base_url=self.config.base_url,
            headers={"Authorization": f"Bearer {self.config.api_token}"},
            timeout=60.0,
        ) as client:
            try:
                resp = await client.post("/polaris/agents/cathedral-eval", json=body)
            except httpx.HTTPError as e:
                raise PolarisRunnerError(f"deploy transport: {e}") from e
            if resp.status_code >= 500:
                raise PolarisRunnerError(f"deploy 5xx: {resp.status_code} {resp.text}")
            if resp.status_code >= 400:
                # 4xx is a permanent error — bundle bad, miner cooked.
                raise PolarisRunnerError(f"deploy 4xx: {resp.status_code} {resp.text}")
            payload = resp.json()
            agent_id = str(payload["polaris_agent_id"])
            run_id = str(payload["polaris_run_id"])

            # Poll until terminal or deadline.
            while True:
                elapsed = (datetime.now(UTC) - start).total_seconds()
                if elapsed > deadline_secs:
                    raise PolarisRunnerError(
                        f"polaris run {run_id} exceeded deadline {deadline_secs}s"
                    )
                try:
                    poll = await client.get(f"/polaris/runs/{run_id}")
                except httpx.HTTPError as e:
                    raise PolarisRunnerError(f"poll transport: {e}") from e
                if poll.status_code >= 500:
                    raise PolarisRunnerError(f"poll 5xx: {poll.status_code}")
                pdata = poll.json()
                status = str(pdata.get("status", "")).lower()
                if status == "success":
                    card = pdata.get("output_card")
                    if not isinstance(card, dict):
                        raise PolarisRunnerError("polaris success but no output_card")
                    duration = int((datetime.now(UTC) - start).total_seconds() * 1000)
                    return PolarisRunResult(
                        polaris_agent_id=agent_id,
                        polaris_run_id=run_id,
                        output_card_json=card,
                        duration_ms=duration,
                        errors=[],
                    )
                if status == "failed":
                    raise PolarisRunnerError(f"polaris run failed: {pdata.get('error', '?')}")
                await asyncio.sleep(self.config.poll_interval_secs)


# --------------------------------------------------------------------------
# PolarisRuntimeRunner — Tier A (Polaris-hosted) miners
# --------------------------------------------------------------------------


#: Canonical signed-payload keys per the Polaris attestation contract.
#: Any drift between Cathedral and Polaris on this set changes the
#: canonical-JSON bytes and breaks signature verification on both sides.
_ATTESTATION_PAYLOAD_KEYS: tuple[str, ...] = (
    "submission_id",
    "task_id",
    "task_hash",
    "output_hash",
    "deployment_id",
    "completed_at",
)


def _canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
    """Sorted-keys, no-whitespace, UTF-8 — pinned signing canonicalization.

    Mirrors `cathedral.v1_types.canonical_json` (which strips
    Cathedral-only post-signing fields). Here the dictionary IS the
    full signed payload; there is nothing to strip.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _blake3_hex(data: bytes) -> str:
    return blake3.blake3(data).hexdigest()


class BundleUrlResolver(Protocol):
    """Returns a presigned URL for the miner's encrypted bundle.

    Production implementation is `HippiusPresignedUrlResolver` (boto3
    `generate_presigned_url`). Tests inject a fake that returns a fixed
    URL so the runtime-evaluate call can be asserted without touching
    object storage.
    """

    def url_for(self, submission: dict[str, Any]) -> str: ...


@dataclass(frozen=True)
class PolarisRuntimeRunnerConfig:
    """Static config the orchestrator builds from env at startup.

    `base_url`         — Polaris API origin (e.g. `https://api.polaris.computer`).
    `api_token`        — Cathedral's service-principal bearer.
    `submission_id`    — Polaris marketplace submission id for the
                         pre-registered Cathedral runtime image.
    `attestation_public_key_hex` — 32-byte hex Ed25519 public key Polaris
                         signs attestations with. Required; no fallback.
    `bundle_url_resolver` — callable that, given the AgentSubmission row,
                         returns a presigned URL Polaris can fetch the
                         encrypted bundle from. Pluggable so unit tests
                         can inject deterministic URLs without touching
                         S3.
    `bundle_encryption_key_hex` — KEK we expose to the Polaris runtime
                         via `env_overrides` so it can decrypt the
                         bundle. v1 weakness documented in CONTRACTS.md;
                         a future revision will move to per-bundle key
                         wrapping signed by Polaris.
    `request_timeout_secs` — HTTP timeout for the runtime-evaluate POST.
                         The Polaris endpoint blocks until the eval
                         finishes (or the runtime hits its internal
                         `timeout_seconds`), so this must accommodate
                         the worst-case eval duration plus headroom.
    `runtime_timeout_secs` — value we pass to Polaris in
                         `timeout_seconds`. Distinct from the HTTP
                         timeout: Polaris kills the eval here, we wait
                         a bit longer to receive the failure response.
    """

    base_url: str
    api_token: str
    submission_id: str
    attestation_public_key_hex: str
    bundle_url_resolver: BundleUrlResolver
    bundle_encryption_key_hex: str = ""
    request_timeout_secs: float = 60.0 * 12  # 12 min HTTP timeout
    runtime_timeout_secs: int = 600  # 10 min Polaris-side budget


class PolarisRuntimeRunner:
    """Tier A runner — Polaris executes the miner's bundle on Cathedral's runtime.

    Flow (CONTRACTS.md §6 step 3, Tier A variant):

      1. Resolve a presigned URL for the miner's encrypted bundle on R2.
         Polaris fetches and decrypts it inside the runtime container.
      2. POST to `/api/marketplace/submissions/{id}/runtime-evaluate`
         with `{task, task_id, timeout_seconds, env_overrides}`. The
         response is synchronous from Cathedral's perspective: Polaris
         deploys, runs, captures output, then returns.
      3. Validate the attestation: recompute `task_hash` and
         `output_hash`, confirm both match the signed payload, then
         verify the Ed25519 signature over `canonical_json(payload)`
         using the configured public key.
      4. Decode the output (base64-then-JSON, or pre-parsed `output_json`)
         into a Card dict and return it. The scoring pipeline runs
         exactly as it does for the other runners — the only difference
         is that `output_card_json` came from a verified runtime.

    Failure modes (all surface as `PolarisRunnerError` subclasses so the
    orchestrator's existing retry path applies uniformly):

      - HTTP transport or 5xx        -> PolarisRunnerError
      - 4xx                          -> PolarisRunnerError (terminal-ish; retry until quota)
      - hash mismatch                -> PolarisAttestationError
      - bad signature                -> PolarisAttestationError
      - malformed output bytes       -> PolarisRunnerError
      - missing/non-dict output      -> PolarisRunnerError
    """

    def __init__(self, config: PolarisRuntimeRunnerConfig) -> None:
        self.config = config
        try:
            raw = bytes.fromhex(config.attestation_public_key_hex.strip())
        except ValueError as e:
            raise PolarisRunnerError("POLARIS_ATTESTATION_PUBLIC_KEY must be hex-encoded") from e
        if len(raw) != 32:
            raise PolarisRunnerError(
                f"POLARIS_ATTESTATION_PUBLIC_KEY must be 32 bytes, got {len(raw)}"
            )
        self._verify_key = Ed25519PublicKey.from_public_bytes(raw)

    async def run(
        self,
        *,
        bundle_bytes: bytes,
        bundle_hash: str,
        task: EvalTask,
        miner_hotkey: str,
        submission: dict[str, Any] | None = None,
    ) -> PolarisRunResult:
        del bundle_bytes  # Polaris fetches the bundle itself via presigned URL.
        del miner_hotkey  # scoring_pipeline re-pins attribution server-side.

        # Prefer the real submission row when the orchestrator passed it.
        # The fallback shim is for unit tests that mock the runner without
        # plumbing through a DB row.
        if submission is None:
            submission = self._submission_from_task(task, bundle_hash)
        bundle_url = self.config.bundle_url_resolver.url_for(submission)

        # Cathedral correlates the deployment via the EvalTask coordinates;
        # the bundle_hash is informational only (Polaris's attestation
        # binds the runtime to its own deployment_id, not the bundle hash).
        task_id = f"cathedral-{task.card_id}-e{task.epoch}r{task.round_index}"

        env_overrides: dict[str, str] = {
            "CARD_ID": task.card_id,
            "MINER_BUNDLE_URL": bundle_url,
        }
        if self.config.bundle_encryption_key_hex:
            # v1: ship the bundle KEK as an env override. CONTRACTS.md §7.4
            # flags this as a known weakness — future revision will wrap
            # per-bundle data keys with a Polaris-side KMS key.
            env_overrides["CATHEDRAL_BUNDLE_KEK"] = self.config.bundle_encryption_key_hex
        # Per-bundle encryption_key_id carries the wrapped data key + nonce
        # that the runtime needs alongside the KEK to actually decrypt the
        # ciphertext from R2. Pulled from the agent_submissions row.
        if submission and (kid := submission.get("encryption_key_id")):
            env_overrides["CATHEDRAL_BUNDLE_KEY_ID"] = str(kid)
        # LLM provider key. Polaris's marketplace eval pipeline doesn't
        # currently project the host's vault into the spawned runtime
        # container (see polaris/services/marketplace_eval.py — env_vars
        # field is not threaded through). Until Polaris ships that
        # injection, Cathedral passes the configured CHUTES_API_KEY
        # through directly. The trust boundary is already inside our
        # operator scope (we are the trusted-service principal), so
        # this doesn't leak outside our control plane.
        import os as _os

        if chutes := _os.environ.get("CHUTES_API_KEY"):
            env_overrides["CHUTES_API_KEY"] = chutes
        if base_url := _os.environ.get("CHUTES_BASE_URL"):
            env_overrides["CHUTES_BASE_URL"] = base_url

        logger.info(
            "polaris_runtime_dispatch",
            task_id=task_id,
            bundle_url_host=bundle_url.split("?")[0][:120],
            env_keys=sorted(env_overrides.keys()),
        )

        body = {
            "task": task.prompt,
            "task_id": task_id,
            "timeout_seconds": self.config.runtime_timeout_secs,
            "env_overrides": env_overrides,
        }
        start = datetime.now(UTC)

        async with httpx.AsyncClient(
            base_url=self.config.base_url,
            headers={"Authorization": f"Bearer {self.config.api_token}"},
            timeout=self.config.request_timeout_secs,
        ) as client:
            try:
                resp = await client.post(
                    f"/api/marketplace/submissions/{self.config.submission_id}/runtime-evaluate",
                    json=body,
                )
            except httpx.HTTPError as e:
                raise PolarisRunnerError(f"runtime-evaluate transport: {e}") from e

        if resp.status_code >= 500:
            raise PolarisRunnerError(f"runtime-evaluate 5xx: {resp.status_code} {resp.text[:512]}")
        if resp.status_code >= 400:
            # 4xx is permanent (bad submission id, expired token, malformed
            # body). Still surface as retryable so the orchestrator applies
            # its uniform 3-attempt policy and the operator sees the failure
            # in the eval_run errors list rather than a silent hang.
            raise PolarisRunnerError(f"runtime-evaluate 4xx: {resp.status_code} {resp.text[:512]}")

        try:
            payload = resp.json()
        except ValueError as e:
            raise PolarisRunnerError(f"runtime-evaluate non-JSON response: {e}") from e

        status_str = str(payload.get("status", "")).lower()
        if status_str and status_str != "succeeded":
            raise PolarisRunnerError(
                f"runtime-evaluate status={status_str!r}: "
                f"{payload.get('error', payload.get('message', '?'))}"
            )

        attestation = self._verify_attestation(payload=payload, task=task, task_id=task_id)
        card_dict = self._decode_card(payload)
        duration_ms = int((datetime.now(UTC) - start).total_seconds() * 1000)
        polaris_duration_ms = payload.get("duration_ms")
        if isinstance(polaris_duration_ms, int):
            # Prefer Polaris-reported duration when present — it isolates the
            # eval runtime from Cathedral-side network + verification overhead.
            duration_ms = polaris_duration_ms

        deployment_id = str(payload.get("deployment_id") or "")
        agent_id = f"polaris-runtime:{deployment_id}" if deployment_id else "polaris-runtime"
        run_id = str(payload.get("task_id") or task_id)

        return PolarisRunResult(
            polaris_agent_id=agent_id,
            polaris_run_id=run_id,
            output_card_json=card_dict,
            duration_ms=duration_ms,
            errors=[],
            attestation=attestation,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _submission_from_task(self, task: EvalTask, bundle_hash: str) -> dict[str, Any]:
        """Minimal shim so resolvers can be unit-tested without a DB row.

        The orchestrator passes the real `AgentSubmission` row through
        `bundle_url_resolver.url_for` in production. Here we expose the
        fields a resolver actually needs (`bundle_hash`, `card_id`,
        `epoch`, `round_index`). Resolvers should treat unknown keys as
        opaque and not depend on the full submission shape.
        """
        return {
            "bundle_hash": bundle_hash,
            "card_id": task.card_id,
            "epoch": task.epoch,
            "round_index": task.round_index,
        }

    def _verify_attestation(
        self,
        *,
        payload: dict[str, Any],
        task: EvalTask,
        task_id: str,
    ) -> PolarisAttestation:
        att_raw = payload.get("attestation")
        if not isinstance(att_raw, dict):
            raise PolarisAttestationError("attestation missing or not an object")
        try:
            version = str(att_raw["version"])
            att_payload = att_raw["payload"]
            signature_b64 = str(att_raw["signature"])
            public_key_hex = str(att_raw["public_key"])
        except KeyError as e:
            raise PolarisAttestationError(f"attestation missing field: {e}") from e
        if not isinstance(att_payload, dict):
            raise PolarisAttestationError("attestation.payload must be an object")
        missing = [k for k in _ATTESTATION_PAYLOAD_KEYS if k not in att_payload]
        if missing:
            raise PolarisAttestationError(f"attestation payload missing fields: {missing}")

        # Pin the public key to the one we configured — accepting any
        # key the response advertises would let a downstream MITM
        # substitute a key it controls. We still surface the response key
        # for log inspection but verify against ours.
        if public_key_hex.strip().lower() != self.config.attestation_public_key_hex.strip().lower():
            raise PolarisAttestationError(
                "attestation public_key does not match configured POLARIS_ATTESTATION_PUBLIC_KEY"
            )

        # Re-derive task_hash from the prompt we sent. Polaris MUST hash
        # the exact `task` string we POSTed; any rewriting on Polaris's
        # side breaks this and is a contract violation we want to catch.
        expected_task_hash = _blake3_hex(task.prompt.encode("utf-8"))
        if att_payload["task_hash"] != expected_task_hash:
            raise PolarisAttestationError(
                f"task_hash mismatch: attestation={att_payload['task_hash']!r} "
                f"expected={expected_task_hash!r}"
            )

        # Re-derive output_hash from the bytes we'll actually decode.
        output_bytes = self._raw_output_bytes(payload)
        expected_output_hash = _blake3_hex(output_bytes)
        if att_payload["output_hash"] != expected_output_hash:
            raise PolarisAttestationError(
                f"output_hash mismatch: attestation={att_payload['output_hash']!r} "
                f"expected={expected_output_hash!r}"
            )

        # Cross-check task_id — Polaris must echo back the id we sent.
        if str(att_payload["task_id"]) != task_id:
            raise PolarisAttestationError(
                f"task_id mismatch: attestation={att_payload['task_id']!r} expected={task_id!r}"
            )

        # Cross-check submission_id — must match the runtime image we registered.
        if str(att_payload["submission_id"]) != self.config.submission_id:
            raise PolarisAttestationError(
                f"submission_id mismatch: attestation="
                f"{att_payload['submission_id']!r} "
                f"expected={self.config.submission_id!r}"
            )

        # Ed25519 verify over canonical(payload).
        try:
            signature = base64.b64decode(signature_b64, validate=True)
        except (ValueError, binascii.Error) as e:
            raise PolarisAttestationError(f"attestation signature not base64: {e}") from e
        try:
            self._verify_key.verify(signature, _canonical_payload_bytes(att_payload))
        except InvalidSignature as e:
            raise PolarisAttestationError(
                "attestation signature does not verify against POLARIS_ATTESTATION_PUBLIC_KEY"
            ) from e

        return PolarisAttestation(
            version=version,
            payload=dict(att_payload),
            signature=signature_b64,
            public_key=public_key_hex,
        )

    def _raw_output_bytes(self, payload: dict[str, Any]) -> bytes:
        """The bytes the attestation's `output_hash` covers.

        Polaris hashes the BASE64-DECODED output buffer. `output_json`
        is a parsed convenience; the hashed source is always
        `base64-decode(payload["output"])`. If `output` is missing we
        fall back to canonical JSON of `output_json` so unit tests that
        skip the base64 round-trip can still attest cleanly.
        """
        raw_b64 = payload.get("output")
        if isinstance(raw_b64, str) and raw_b64:
            try:
                return base64.b64decode(raw_b64, validate=True)
            except (ValueError, binascii.Error) as e:
                raise PolarisRunnerError(f"runtime-evaluate output not base64: {e}") from e
        oj = payload.get("output_json")
        if isinstance(oj, dict):
            return _canonical_payload_bytes(oj)
        raise PolarisRunnerError("runtime-evaluate response has neither 'output' nor 'output_json'")

    def _decode_card(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Prefer `output_json` if it's already a dict; otherwise decode.

        Polaris's runtime-evaluate wraps the runtime container's response
        in its own envelope, so `payload['output_json']` is the runtime's
        FULL response (e.g. `{output_json: <Card>, duration_ms, model}`).
        Unwrap one more level when we detect this — the runtime contract
        is that it itself returns `{output_json: <Card>, ...}`.
        """
        oj = payload.get("output_json")
        if isinstance(oj, dict):
            inner = oj.get("output_json")
            # If the runtime double-wrapped, drill in once. The Card
            # schema requires `last_refreshed_at` and `refresh_cadence_hours`,
            # so any dict missing those is almost certainly the runtime's
            # envelope, not the Card.
            if isinstance(inner, dict) and ("last_refreshed_at" in inner or "citations" in inner):
                return dict(inner)
            return dict(oj)
        raw = self._raw_output_bytes(payload)
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as e:
            raise PolarisRunnerError(f"runtime-evaluate output not utf-8: {e}") from e
        except json.JSONDecodeError as e:
            raise PolarisRunnerError(f"runtime-evaluate output not valid JSON: {e}") from e
        if not isinstance(decoded, dict):
            raise PolarisRunnerError(
                f"runtime-evaluate output must be a JSON object, got {type(decoded).__name__}"
            )
        # Also handle the wrapped shape via the bytes path.
        inner = decoded.get("output_json")
        if isinstance(inner, dict) and ("last_refreshed_at" in inner or "citations" in inner):
            return dict(inner)
        return decoded


class HippiusPresignedUrlResolver:
    """Default `BundleUrlResolver` — boto3 `generate_presigned_url`.

    Wraps the existing `HippiusClient` so Polaris can `GET` the encrypted
    blob without Cathedral having to stream it. The presigned URL expires
    after `expires_in` seconds (default 1 hour — enough for Polaris to
    deploy and fetch even on a cold node).

    The bundle ciphertext stays encrypted at rest; the runtime decrypts
    it in-process using `CATHEDRAL_BUNDLE_KEK` injected via
    `env_overrides` (see PolarisRuntimeRunnerConfig).
    """

    def __init__(self, hippius: Any, *, expires_in: int = 3600) -> None:
        self._hippius = hippius
        self._expires_in = expires_in

    def url_for(self, submission: dict[str, Any]) -> str:
        # Diagnostic: ensure bundle_blob_key actually carries the s3 uri.
        # In prod we saw the URL fall through to bundle_hash even though
        # submit.py writes a real blob_key.
        bbk = submission.get("bundle_blob_key")
        bh = submission.get("bundle_hash")
        logger.info(
            "presign_resolver",
            sub_id=submission.get("id"),
            row_keys=sorted(submission.keys()),
            bundle_blob_key=bbk,
            bundle_blob_key_type=type(bbk).__name__,
            bundle_hash_short=bh[:16] if isinstance(bh, str) else None,
        )
        key = bbk or bh or ""
        if not key:
            raise PolarisRunnerError(
                "submission has no bundle_blob_key — cannot presign for Polaris"
            )
        try:
            return self._hippius.presigned_get_url(  # type: ignore[no-any-return]
                key, expires_in=self._expires_in
            )
        except AttributeError as e:
            raise PolarisRunnerError(
                "HippiusClient.presigned_get_url not available — upgrade "
                "the storage client or wire a different BundleUrlResolver"
            ) from e
