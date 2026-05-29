"""Base runner types shared by the live SAT prober and KEEP importers.

These types were extracted verbatim from ``eval/polaris_runner.py`` so the
SAT-serving lane (``ssh_hermes_runner`` and friends) can depend on the runner
error/result/protocol contract without importing the card-runner machinery that
is being stripped. ``polaris_runner`` re-exports these names so every existing
import path keeps resolving during the strip.

Holds exactly the six base types the SAT prober + KEEP importers need:
``PolarisRunnerError``, ``PolarisAttestationError``, ``PolarisAttestation``,
``PolarisRunResult``, ``PolarisRunner`` (Protocol), and ``StubPolarisRunner``
(directly imported by KEEP lane tests). ``_juris_for`` comes along so
``StubPolarisRunner`` is self-contained.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from cathedral.v1_types import EvalTask


class PolarisRunnerError(Exception):
    """Polaris call failed in a retryable or terminal way."""


class PolarisAttestationError(PolarisRunnerError):
    """Polaris returned an attestation that does not verify.

    Surfaces as a runner failure to the orchestrator — the eval is not
    scored, the submission's retry counter advances, and after 3 such
    failures the submission is marked `rejected` with
    `rejection_reason="polaris exhausted retries"` (CONTRACTS.md §6).
    """


@dataclass(frozen=True)
class PolarisAttestation:
    """Signed proof from Polaris that the eval really ran on its runtime.

    Mirrors the body of the `attestation` field returned by
    `POST /api/marketplace/submissions/{id}/runtime-evaluate`. The
    `payload` keys are pinned by the Polaris-side contract: changing
    the set of fields changes the canonical signed bytes and breaks
    verification on both sides.
    """

    version: str
    payload: dict[str, Any]
    signature: str  # base64
    public_key: str  # hex

    def to_storage_dict(self) -> dict[str, Any]:
        """JSON-stable shape persisted on the `eval_runs` row."""
        return {
            "version": self.version,
            "payload": self.payload,
            "signature": self.signature,
            "public_key": self.public_key,
        }


@dataclass
class PolarisRunResult:
    polaris_agent_id: str
    polaris_run_id: str
    output_card_json: dict[str, Any]
    duration_ms: int
    errors: list[str] = field(default_factory=list)
    attestation: PolarisAttestation | None = None
    # Stage B.4: long-lived probe runs sign with the miner's sr25519 hotkey
    # rather than the Polaris attestation key. When this field is set, the
    # orchestrator persists it alongside the Polaris attestation on the
    # eval_runs row.
    probe_attestation: dict[str, Any] | None = None
    # v2.0: structured Hermes trace captured by `PolarisDeployRunner`.
    # None for legacy runners (Stub / Bundle / Runtime). Persisted in
    # `eval_runs.trace_json` as an unsigned sidecar — additive,
    # backward-compatible, doesn't change the canonical signed bytes.
    trace: dict[str, Any] | None = None
    # v2.0: signed Polaris manifest fetched after the deploy. None when
    # the runner couldn't reach the manifest endpoint (transport error)
    # or when the agent owner has no registered TAO address. Used by
    # the orchestrator to flag `polaris_verified=True` on the row.
    manifest: dict[str, Any] | None = None
    # v1.1.0 PR 5: when the runner produced a Hermes trace bundle on
    # local disk (currently only SshHermesRunner does this), the
    # orchestrator picks it up and hands it to EvalArtifactPublisher
    # for Hippius upload + manifest signing. Other runners leave this
    # None. Forward-declared as Any to avoid a circular import with
    # ssh_hermes_runner.TraceBundle.
    trace_bundle: Any | None = None


class PolarisRunner(Protocol):
    async def run(
        self,
        *,
        bundle_bytes: bytes,
        bundle_hash: str,
        task: EvalTask,
        miner_hotkey: str,
        submission: dict[str, Any] | None = None,
    ) -> PolarisRunResult: ...


def _juris_for(card_id: str) -> str:
    if card_id.startswith("eu-"):
        return "eu"
    if card_id.startswith("us-"):
        return "us"
    if card_id.startswith("uk-"):
        return "uk"
    if card_id.startswith("singapore-"):
        return "sg"
    if card_id.startswith("japan-"):
        return "jp"
    return "other"


# --------------------------------------------------------------------------
# Stub for tests / dev
# --------------------------------------------------------------------------


class StubPolarisRunner:
    """Returns a hand-crafted card so the rest of the pipeline can run.

    Use when `CATHEDRAL_EVAL_MODE=stub`. The card it returns will pass
    preflight (single citation, no_legal_advice=true, etc) but its
    score is intentionally middling so first-mover delta logic can be
    exercised.
    """

    def __init__(self, *, fixed_score_seed: int = 0) -> None:
        self._counter = fixed_score_seed
        # When set (via build_app for CATHEDRAL_EVAL_MODE=stub-deterministic-score
        # mode + CATHEDRAL_STUB_SCORE env var), the stub fabricates a card
        # with content tuned to score approximately at this value. Used by
        # the first-mover delta integration test which needs two
        # submissions to score identically.
        import os

        score_env = os.environ.get("CATHEDRAL_STUB_SCORE")
        try:
            self._target_score: float | None = float(score_env) if score_env else None
        except (TypeError, ValueError):
            self._target_score = None

    async def run(
        self,
        *,
        bundle_bytes: bytes,
        bundle_hash: str,
        task: EvalTask,
        miner_hotkey: str,
        submission: dict[str, Any] | None = None,
    ) -> PolarisRunResult:
        self._counter += 1
        agent_id = f"agt_stub_{task.card_id}_{self._counter:04d}"
        run_id = f"run_stub_{uuid4().hex[:12]}"

        now = datetime.now(UTC)
        card = {
            "id": task.card_id,
            "jurisdiction": _juris_for(task.card_id),
            "topic": "stub eval output",
            "worker_owner_hotkey": miner_hotkey,
            "polaris_agent_id": agent_id,
            "title": f"Stub: {task.prompt[:40]}",
            "summary": (
                "Stubbed eval output — used in CATHEDRAL_EVAL_MODE=stub for "
                "end-to-end smoke tests of the publisher + scoring pipeline. "
                "Replace with real Polaris-spawned Hermes output in production."
            ),
            "what_changed": "no real change captured; this is a stub",
            "why_it_matters": (
                "The eval orchestrator's scoring pipeline runs against this "
                "fabricated card so we can verify the full submission ->\n"
                "encrypt -> queue -> stub-eval -> score -> sign chain works "
                "without depending on a live Polaris API."
            ),
            "action_notes": "ignore in production",
            "risks": "stub mode must never run on prod; gate by env",
            "citations": [
                {
                    "url": "https://example.invalid/stub",
                    "class": "other",
                    "fetched_at": now.isoformat(),
                    "status": 200,
                    "content_hash": "0" * 64,
                }
            ],
            "confidence": 0.6,
            "no_legal_advice": True,
            "last_refreshed_at": now.isoformat(),
            "refresh_cadence_hours": 24,
        }
        return PolarisRunResult(
            polaris_agent_id=agent_id,
            polaris_run_id=run_id,
            output_card_json=card,
            duration_ms=12,
            errors=[],
        )
