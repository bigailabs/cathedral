"""CathedralEngine -- the v4 publisher-side challenge runtime.

**Architectural position (REVISED 2026-05-17):**

v4 is a **publisher-side private challenge runtime**. It runs on the
publisher's worker host. It is NOT embedded in the validator loop.

The end-to-end flow:

  1. Publisher loads a synthetic row from private operator storage
     (``CATHEDRAL_V4_CORPUS_PATH``) -- never from the public repo.
  2. Publisher calls
     ``engine.build_miner_bundle(task)`` which loads the upstream
     micro-repo from ``vault/``, scrambles it, **applies the bug
     patch server-side**, and emits a tarball/dict of broken-state
     files that the miner can read. **The raw bug patch is never
     shipped to the miner.**
  3. Publisher gives the broken bundle to a miner and receives back a
     unified-diff fix patch plus the recorded tool trace.
  4. Publisher calls
     ``engine.verify_miner_submission(broken_state, fix_patch,
     hidden_test)`` which runs in the bounded publisher-side oracle
     (network-isolated, rlimit-bounded subprocess on tmpfs scratch).
  5. Publisher calls
     ``engine.package_elite_telemetry(raw_payload)`` to produce the
     canonical ``ValidationPayload`` envelope with deterministic
     hash.
  6. Publisher signs the row via ``cathedral.v4.sign.build_signed_v4_row``
     (which delegates to the existing v3 ``EvalSigner`` rather than
     reinventing signing) and persists it. The validator pulls the
     signed row, verifies the signature, and reads the score. **The
     validator never executes any of this code.**

Two perf budgets (per the revised spec, see
``cathedral.v4.oracle.patch_runner``):

  * ``BOOKKEEPING_BUDGET_SECONDS = 0.20`` -- engine-only bookkeeping
    (load, scramble, build bundle, hash, package telemetry, sign).
    Asserted by ``tests/v4/test_benchmark.py::test_bookkeeping_*``.
  * ``REPRO_BUDGET_SECONDS = 3.0`` -- the actual subprocess that runs
    the hidden test on the publisher worker. Asserted by
    ``tests/v4/test_benchmark.py::test_repro_*``.

**Siphon rule.** Trajectories with a single-turn SUCCESS outcome are
flagged: a one-shot fix has no multi-step trace, which means the
publisher cannot learn anything from it for the data moat, and it is
the canonical answer-sharing signature. The flag is computed in
``siphon_flags_for(payload)`` and not baked into the payload itself
(payloads are immutable once signed; flagging is publisher-side
audit metadata).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from cathedral.v4.arena.sandbox import (
    IsomorphicScrambler,
    MinerArena,
    ScrambledRepo,
    _apply_unified_diff,
    _DiffError,
)
from cathedral.v4.oracle.patch_runner import (
    BOOKKEEPING_BUDGET_SECONDS,
    REPRO_BUDGET_SECONDS,
    PatchRunResult,
    run_patch_against_hidden_test,
)
from cathedral.v4.schemas import (
    AgentTurn,
    MinerBundle,
    MinerTrajectory,
    ValidationPayload,
)

logger = structlog.get_logger(__name__)


# Siphon rule constants. Centralized here so tests + audit log share them.
ONE_SHOT_TURN_THRESHOLD: int = 1
SIPHON_FLAG_ONE_SHOT: str = "one_shot_no_trace"


class EngineError(Exception):
    """Raised on operator/wiring errors that the engine cannot recover
    from (missing vault, missing corpus path, malformed task row).
    """


class PublisherHandle(BaseModel):
    """Publisher-internal handle held alongside every miner bundle.

    Carries every field the publisher needs to drive the oracle
    (clean_state, rename_map, hidden_test_code, winning_patch) and
    that MUST NEVER cross the miner boundary. Pairs with the
    wire-safe ``cathedral.v4.schemas.MinerBundle``.

    The type system enforces the split: a transport that handles
    ``MinerBundle`` cannot accidentally pick up the answer because
    the answer lives in a different object that the engine returns
    separately from ``build_publisher_handle``.

    Added 2026-05-17 (Finding 2, PR #133 review). Previously the
    engine returned a single ``dict`` containing both broken_state
    and clean_state; the miner-facing method name made it easy for
    a downstream transport to serialize the whole return value.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    task_id: str
    base_repo: str
    language: str
    seed: int
    workspace_path: str
    clean_state: dict[str, str] = Field(
        ..., description="Pre-bug scrambled file contents (the answer key)"
    )
    rename_map: dict[str, str] = Field(default_factory=dict)
    file_rename_map: dict[str, str] = Field(default_factory=dict)
    string_rotation: dict[str, str] = Field(default_factory=dict)
    compile_command: list[str] = Field(default_factory=list)
    test_entry_path: str | None = None


class CathedralEngine:
    """v4 publisher-side orchestrator.

    Lifecycle:

      * ``__init__(vault_path, corpus_path=None)`` -- anchor the engine
        to a vault dir + optional private corpus dir. If
        ``corpus_path`` is None we read ``CATHEDRAL_V4_CORPUS_PATH``
        from the environment; if neither is set, ``load_task`` raises.
      * ``load_task(task_id)`` -- fetch one private row from corpus.
      * ``load_and_scramble_task(base_repo)`` -- synthesize a fresh
        ad-hoc task from a vault upstream base (for tests and
        scaffolding; production uses ``load_task``).
      * ``build_miner_bundle(task)`` -- apply the synthetic bug
        server-side and return the broken-state bundle the miner
        sees. The raw bug patch is NOT included.
      * ``verify_miner_submission(...)`` -- bounded publisher oracle.
      * ``package_elite_telemetry(raw)`` -- canonical envelope + hash.
    """

    def __init__(
        self,
        vault_path: str = "./vault",
        corpus_path: str | None = None,
    ) -> None:
        self.vault_path = Path(vault_path).resolve()
        self._scrambler = IsomorphicScrambler(self.vault_path)
        self._workspace_root = Path(tempfile.mkdtemp(prefix="v4_engine_"))
        # Corpus path is optional at engine-construction time so unit
        # tests that only exercise the scrambler/oracle path don't
        # need a corpus dir. ``load_task`` is the only call that
        # demands it.
        env_corpus = os.environ.get("CATHEDRAL_V4_CORPUS_PATH")
        self._corpus_path: Path | None
        if corpus_path is not None:
            self._corpus_path = Path(corpus_path).resolve()
        elif env_corpus:
            self._corpus_path = Path(env_corpus).resolve()
        else:
            self._corpus_path = None
        logger.info(
            "v4.engine.init",
            vault_path=str(self.vault_path),
            corpus_path=str(self._corpus_path) if self._corpus_path else None,
            workspace_root=str(self._workspace_root),
        )

    # ------------------------------------------------------------------
    # corpus / task loading
    # ------------------------------------------------------------------
    def load_task(self, task_id: str) -> dict[str, Any]:
        """Load one synthetic task row from the private corpus.

        Corpus rows live OUTSIDE the public repo. The publisher
        operator sets ``CATHEDRAL_V4_CORPUS_PATH`` to the directory
        that holds them. Each row is a JSON file named
        ``<task_id>.json`` with at minimum:

          * ``task_id``: stable opaque ID
          * ``base_repo``: vault subdir to scramble
          * ``seed``: scrambler seed (deterministic)
          * ``difficulty_tier``: bronze / silver / gold
          * ``injected_fault_type``: short tag
          * ``bug_patch``: unified diff applied server-side BEFORE
            the bundle is built for the miner
          * ``winning_patch_template``: ``{{rename:<id>}}``-templated
            diff that fixes the bug; signed into the row so the
            validator can later replay
          * ``hidden_test_template``: ``{{rename:<id>}}``-templated
            Python source for the hidden test
        """
        if self._corpus_path is None:
            raise EngineError(
                "no corpus path configured: pass corpus_path=... or set CATHEDRAL_V4_CORPUS_PATH"
            )
        row_path = self._corpus_path / f"{task_id}.json"
        if not row_path.is_file():
            raise EngineError(f"no such task row: {row_path}")
        data: dict[str, Any] = json.loads(row_path.read_text())
        for required in (
            "task_id",
            "base_repo",
            "seed",
            "difficulty_tier",
            "injected_fault_type",
            "bug_patch",
            "winning_patch_template",
            "hidden_test_template",
        ):
            if required not in data:
                raise EngineError(f"task row {task_id} missing field {required!r}")
        return data

    # ------------------------------------------------------------------
    # ad-hoc task synthesis (no private corpus; used by tests/scaffolding)
    # ------------------------------------------------------------------
    def load_and_scramble_task(self, base_repo: str) -> dict[str, Any]:
        """Synthesize a fresh ad-hoc task from a vault upstream base.

        Used by tests and dev scaffolding. Production flow is
        ``load_task(task_id)`` + ``build_miner_bundle(task)``.

        The returned dict contains the publisher-internal handle
        (the original clean state, rename maps, scrambler seed,
        compile command, and a ready-to-use ``MinerArena`` bound to
        the scrambled workspace).
        """
        seed = secrets.randbits(64)
        scrambled = self._scrambler.scramble(
            base_repo=base_repo,
            seed=seed,
            workspace_root=self._workspace_root,
        )
        arena = MinerArena(scrambled)
        task_id = f"v4t_{seed:016x}"
        return {
            "task_id": task_id,
            "base_repo": base_repo,
            "seed": seed,
            "language": scrambled.language,
            "workspace_path": str(scrambled.workspace_path),
            "original_repo_state": dict(scrambled.files),
            "rename_map": dict(scrambled.rename_map),
            "file_rename_map": dict(scrambled.file_rename_map),
            "string_rotation": dict(scrambled.string_rotation),
            "compile_command": list(scrambled.compile_command),
            "test_entry_path": scrambled.test_entry_path,
            "arena": arena,
        }

    # ------------------------------------------------------------------
    # bundle for the miner (apply bug server-side)
    # ------------------------------------------------------------------
    def build_bundle_and_handle(
        self,
        base_repo: str,
        bug_patch: str,
        seed: int | None = None,
        difficulty_tier: str = "bronze",
        task_id: str | None = None,
    ) -> tuple[MinerBundle, PublisherHandle]:
        """Scramble, apply the bug, return the (bundle, handle) pair.

        This is the single source of truth for "what the miner gets"
        vs "what the publisher keeps". Two distinct typed objects;
        every call site that needs both names them both explicitly.

          * ``MinerBundle`` -- broken-state workspace + public task
            descriptors. Wire safe. Send to the miner.
          * ``PublisherHandle`` -- clean state + rename maps. Stays
            on the publisher. Never serialized to the miner.

        Server-side, we:

          1. Load ``base_repo`` from the vault.
          2. Scramble it deterministically under ``seed``.
          3. Apply the ``bug_patch`` to the scrambled state.

        Raises ``EngineError`` if ``bug_patch`` fails to apply (a
        publisher-side wiring bug; the bug patch must always apply
        to its declared base).

        Added 2026-05-17 (Finding 2, PR #133 review).
        """
        seed = seed if seed is not None else secrets.randbits(64)
        scrambled = self._scrambler.scramble(
            base_repo=base_repo,
            seed=seed,
            workspace_root=self._workspace_root,
        )
        try:
            broken = _apply_unified_diff(dict(scrambled.files), bug_patch)
        except _DiffError as e:
            raise EngineError(
                f"bug_patch failed to apply against scrambled {base_repo!r} seed={seed:x}: {e}"
            ) from e

        # Flush broken state to disk so a separate transport (tar,
        # rsync, signed-url upload) can ship it.
        for relpath, content in broken.items():
            dest = scrambled.workspace_path / relpath
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)

        resolved_task_id = task_id or f"v4t_{seed:016x}"
        bundle = MinerBundle(
            task_id=resolved_task_id,
            base_repo=base_repo,
            language=scrambled.language,
            difficulty_tier=difficulty_tier,
            seed=seed,
            workspace_files=broken,
            compile_command=list(scrambled.compile_command),
            test_entry_path=scrambled.test_entry_path,
        )
        handle = PublisherHandle(
            task_id=resolved_task_id,
            base_repo=base_repo,
            language=scrambled.language,
            seed=seed,
            workspace_path=str(scrambled.workspace_path),
            clean_state=dict(scrambled.files),
            rename_map=dict(scrambled.rename_map),
            file_rename_map=dict(scrambled.file_rename_map),
            string_rotation=dict(scrambled.string_rotation),
            compile_command=list(scrambled.compile_command),
            test_entry_path=scrambled.test_entry_path,
        )
        return bundle, handle

    def build_miner_bundle(
        self,
        base_repo: str,
        bug_patch: str,
        seed: int | None = None,
        difficulty_tier: str = "bronze",
        task_id: str | None = None,
    ) -> MinerBundle:
        """Build the broken-state bundle the miner sees.

        Thin wrapper around ``build_bundle_and_handle`` that returns
        ONLY the wire-safe ``MinerBundle``. Callers that also need
        the publisher-internal handle (clean state, rename maps,
        scrambler seed for replay) must call
        ``build_bundle_and_handle`` and bind both halves explicitly.

        The returned bundle deliberately does NOT include
        ``bug_patch``, ``clean_state``, or any rename map. The miner
        only sees broken-state files; the raw bug diff or the
        pre-bug clean state would leak the fault location and
        trivialize the challenge.

        Raises ``EngineError`` if ``bug_patch`` fails to apply.
        """
        bundle, _handle = self.build_bundle_and_handle(
            base_repo=base_repo,
            bug_patch=bug_patch,
            seed=seed,
            difficulty_tier=difficulty_tier,
            task_id=task_id,
        )
        return bundle

    def build_publisher_handle(
        self,
        base_repo: str,
        bug_patch: str,
        seed: int | None = None,
        difficulty_tier: str = "bronze",
        task_id: str | None = None,
    ) -> PublisherHandle:
        """Build the publisher-internal handle (clean state + maps).

        Thin wrapper around ``build_bundle_and_handle`` that returns
        ONLY the ``PublisherHandle``. Use this when the calling site
        is explicitly server-side -- the oracle or the audit logger
        -- and never wants the miner-facing bundle in scope.
        """
        _bundle, handle = self.build_bundle_and_handle(
            base_repo=base_repo,
            bug_patch=bug_patch,
            seed=seed,
            difficulty_tier=difficulty_tier,
            task_id=task_id,
        )
        return handle

    # ------------------------------------------------------------------
    # verification (publisher worker)
    # ------------------------------------------------------------------
    def verify_miner_submission(
        self,
        original_repo_state: dict[str, str],
        patch_str: str,
        hidden_test_code: str,
        timeout_seconds: float = REPRO_BUDGET_SECONDS,
    ) -> tuple[bool, float]:
        """Run the miner's patch against the hidden test on the
        publisher-side oracle.

        Returns ``(passed, duration_seconds)``. ``duration_seconds``
        is wall-clock spent inside the oracle (subprocess + bootstrap
        + patch apply), not including this call's bookkeeping.

        Default ``timeout_seconds`` is the 3s repro budget. For
        lightweight bookkeeping verification a caller can pass the
        tighter ``BOOKKEEPING_BUDGET_SECONDS`` ceiling.
        """
        start = time.monotonic()
        result: PatchRunResult = run_patch_against_hidden_test(
            original_repo_state=original_repo_state,
            patch_str=patch_str,
            hidden_test_code=hidden_test_code,
            timeout_seconds=timeout_seconds,
        )
        total = time.monotonic() - start
        budget = max(timeout_seconds, BOOKKEEPING_BUDGET_SECONDS)
        # Add a small fixed overhead for the surrounding bookkeeping
        # (Popen startup, scratch dir creation) when comparing to budget.
        if total > budget + 0.5:
            logger.warning(
                "v4.oracle.budget_exceeded",
                duration_seconds=total,
                budget=budget,
                timed_out=result.timed_out,
                patch_applied=result.patch_applied,
                isolation_mode=result.isolation_mode,
            )
        return result.passed, total

    # ------------------------------------------------------------------
    # Siphon: telemetry envelope
    # ------------------------------------------------------------------
    def package_elite_telemetry(self, raw_payload: dict[str, Any]) -> str:
        """Wrap ``raw_payload`` in a ``ValidationPayload``, compute the
        deterministic hash, return the canonical-JSON string.

        Siphon flagging (one-shot SUCCESS trajectories) is emitted as
        publisher-side audit log lines and is also available via
        ``siphon_flags_for(payload)``. It is NOT mutated into the
        payload itself; payloads are immutable once signed.

        ``raw_payload`` shape (publisher-internal handoff):

            {
              "task_id": str,
              "difficulty_tier": str,
              "language": str,
              "injected_fault_type": str,
              "winning_patch": str,
              "trajectories": [
                {
                  "miner_hotkey": str,
                  "model_identifier": str,
                  "total_turns": int,
                  "outcome": str,
                  "trace": [
                    {
                      "turn_index": int,
                      "tool_called": str,
                      "arguments": dict,
                      "system_response": str,
                      "duration_ms": int,
                    },
                    ...
                  ],
                },
                ...
              ],
            }
        """
        trajectories: list[MinerTrajectory] = []
        for tj in raw_payload.get("trajectories", []):
            trace = [AgentTurn(**t) for t in tj.get("trace", [])]
            trajectories.append(
                MinerTrajectory(
                    miner_hotkey=tj["miner_hotkey"],
                    model_identifier=tj["model_identifier"],
                    total_turns=int(tj["total_turns"]),
                    outcome=tj["outcome"],
                    trace=trace,
                )
            )

        draft_dict: dict[str, Any] = {
            "task_id": raw_payload["task_id"],
            "difficulty_tier": raw_payload["difficulty_tier"],
            "language": raw_payload["language"],
            "injected_fault_type": raw_payload["injected_fault_type"],
            "winning_patch": raw_payload["winning_patch"],
            "trajectories": [t.model_dump(mode="json") for t in trajectories],
        }
        canonical = json.dumps(draft_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
        det_hash = hashlib.blake2b(canonical, digest_size=32).hexdigest()

        payload = ValidationPayload(
            task_id=raw_payload["task_id"],
            difficulty_tier=raw_payload["difficulty_tier"],
            language=raw_payload["language"],
            injected_fault_type=raw_payload["injected_fault_type"],
            winning_patch=raw_payload["winning_patch"],
            trajectories=trajectories,
            deterministic_hash=det_hash,
        )

        for tj in payload.trajectories:
            if tj.total_turns <= ONE_SHOT_TURN_THRESHOLD and tj.outcome == "SUCCESS":
                logger.info(
                    "v4.siphon.flag",
                    task_id=payload.task_id,
                    miner_hotkey=tj.miner_hotkey,
                    flag=SIPHON_FLAG_ONE_SHOT,
                    total_turns=tj.total_turns,
                )

        return json.dumps(
            payload.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )

    # ------------------------------------------------------------------
    # convenience: identify flagged trajectories without log scraping
    # ------------------------------------------------------------------
    def siphon_flags_for(self, payload: ValidationPayload) -> dict[str, list[str]]:
        """Return ``{miner_hotkey: [flag, ...]}`` for the given payload.

        Pure function. Used by tests and by the publisher's audit log.
        """
        flags: dict[str, list[str]] = {}
        for tj in payload.trajectories:
            tj_flags: list[str] = []
            if tj.total_turns <= ONE_SHOT_TURN_THRESHOLD and tj.outcome == "SUCCESS":
                tj_flags.append(SIPHON_FLAG_ONE_SHOT)
            if tj_flags:
                flags[tj.miner_hotkey] = tj_flags
        return flags


__all__ = [
    "ONE_SHOT_TURN_THRESHOLD",
    "SIPHON_FLAG_ONE_SHOT",
    "CathedralEngine",
    "EngineError",
    "PublisherHandle",
    "ScrambledRepo",
]
