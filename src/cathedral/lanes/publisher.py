"""Publisher helpers for Task Family lanes.

This module bridges SSH Hermes stdout to the pure lane contract. It keeps
the lane author focused on ``generate`` / ``verify`` / ``score`` while the
publisher owns prompts, trace capture, signing, persistence, and feed gates.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import aiosqlite
import blake3

from cathedral.lanes.contract import (
    GenerateCtx,
    PublicProblem,
    ScoreResult,
    Submission,
    TaskFamily,
    VerifierResult,
)
from cathedral.lanes.sign import build_signed_task_family_row, public_task_id
from cathedral.v1_types import canonical_json

_FINAL_ANSWER_RE = re.compile(
    r"```\s*FINAL_ANSWER\b[^\n]*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
_JSON_BLOCK_RE = re.compile(
    r"```\s*json\b[^\n]*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)


class AnswerExtractionError(Exception):
    """The miner stdout did not contain a usable JSON answer object."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class TaskFamilySignedResult:
    row: dict[str, Any]
    verifier: VerifierResult
    score: ScoreResult
    prompt: str
    submission: Submission


def task_family_feed_enabled(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return values.get("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "").lower() == "true"


def enabled_task_family_ids(env: Mapping[str, str] | None = None) -> list[str]:
    values = os.environ if env is None else env
    raw = values.get("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")
    return [item.strip() for item in raw.split(",") if item.strip()]


def task_family_tier(family_id: str, env: Mapping[str, str] | None = None) -> int:
    values = os.environ if env is None else env
    specific_key = f"CATHEDRAL_{family_id.upper()}_TIER"
    raw = values.get(specific_key, values.get("CATHEDRAL_TASK_FAMILY_TIER", "0"))
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def task_family_runner_skip_reason(runner: Any) -> dict[str, str] | None:
    if getattr(runner, "run_task_family_challenge", None) is not None:
        return None
    return {
        "reason": "runner_unsupported",
        "required_runner_interface": "SshHermesRunner.run_task_family_challenge",
        "recommended_env": (
            "CATHEDRAL_EVAL_MODE=ssh-probe "
            "CATHEDRAL_PROBER_VERSION=v2 "
            "CATHEDRAL_TASK_FAMILY_FEED_ENABLED=true"
        ),
    }


def task_family_prober_version_warning(
    env: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    values = os.environ if env is None else env
    if not task_family_feed_enabled(values):
        return None
    if values.get("CATHEDRAL_PROBER_VERSION", "v1").lower() == "v2":
        return None
    return {
        "reason": "prober_version_not_v2",
        "recommended_env": "CATHEDRAL_PROBER_VERSION=v2",
    }


def build_generate_ctx(
    *,
    family_id: str,
    miner_hotkey: str,
    epoch: int,
    round_index: int,
    tier: int,
    issued_at_iso: str,
) -> GenerateCtx:
    seed_material = f"{family_id}:{miner_hotkey}:{epoch}:{round_index}:{tier}"
    digest = blake3.blake3(seed_material.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], byteorder="big", signed=False)
    return GenerateCtx(seed=seed, tier=tier, issued_at_iso=issued_at_iso)


def build_task_family_prompt(problem: PublicProblem) -> str:
    """Render the miner-facing prompt for one task-family problem.

    For the SAT lane the CNF body is no longer inlined: the prompt
    carries ``cnf_url`` + ``cnf_sha256`` so miners fetch the body from
    the publisher's public CNF endpoint and verify integrity locally.
    Inline CNF is retained for the toy/test path (when ``cnf_url`` is
    absent) so existing fixtures keep working.
    """
    payload = problem.model_dump(mode="json")
    public_input = problem.public_input
    has_cnf_url = isinstance(public_input, dict) and isinstance(
        public_input["cnf_url"] if "cnf_url" in public_input else None, str
    )
    if has_cnf_url:
        fetch_section = (
            "Fetch the CNF from the URL in `public_input.cnf_url` with a plain "
            "HTTP GET (no extra headers). Verify the body's sha256 matches "
            "`public_input.cnf_sha256` before solving; abort the challenge "
            "cleanly if it does not match. A 404 response means the challenge "
            "is unavailable; give up on this challenge rather than retrying. "
            "The URL is single-purpose (CNF body only) and is gated by an "
            "unguessable token already embedded in the URL: never log the URL "
            "or its query string."
        )
    else:
        fetch_section = "The CNF body is inlined in `public_input.cnf` for this challenge."
    return (
        f"Capability: {problem.task_family}\n\n"
        "Solve the boolean challenge described below. You may use any tools "
        "available in your Hermes environment. Cathedral verifies only the "
        "final answer, not your prose.\n\n"
        f"{fetch_section}\n\n"
        "Challenge metadata:\n"
        f"{json.dumps(payload, sort_keys=True, indent=2)}\n\n"
        "Return exactly one fenced FINAL_ANSWER JSON block. The JSON object "
        "inside the fence must be the answer payload for this task family. "
        "For the boolean family this is the solver-style DIMACS solution: "
        "an `s SATISFIABLE` line followed by one or more `v <lit> ... 0` "
        "lines covering every variable.\n"
        "```FINAL_ANSWER\n"
        "{\n"
        '  "dimacs_solution": "s SATISFIABLE\\nv 1 -2 3 0\\n"\n'
        "}\n"
        "```\n\n"
        "Do not include prose outside the FINAL_ANSWER block."
    )


def extract_answer(stdout: str) -> dict[str, Any]:
    if not isinstance(stdout, str) or not stdout.strip():
        raise AnswerExtractionError("no_json_block_found", "stdout empty")

    final_blocks = list(_FINAL_ANSWER_RE.finditer(stdout))
    if len(final_blocks) > 1:
        raise AnswerExtractionError(
            "multiple_final_answer_blocks",
            f"found {len(final_blocks)} FINAL_ANSWER blocks",
        )
    if final_blocks:
        return _decode_json(final_blocks[0].group(1), source="FINAL_ANSWER")

    json_blocks = list(_JSON_BLOCK_RE.finditer(stdout))
    if json_blocks:
        return _decode_json(json_blocks[-1].group(1), source="json_fence")

    obj = _scan_last_json_object(stdout)
    if obj is not None:
        return obj

    raise AnswerExtractionError("no_json_block_found", "no fenced or balanced JSON")


def score_and_sign_task_family_stdout(
    *,
    lane: TaskFamily,
    problem: PublicProblem,
    hidden: Any,
    submission_row: dict[str, Any],
    stdout: str,
    ran_at_iso: str,
    signer: Any,
    eval_run_id: str | None = None,
    epoch_salt: str,
    score_override: ScoreResult | None = None,
) -> TaskFamilySignedResult:
    prompt = build_task_family_prompt(problem)
    try:
        answer = extract_answer(stdout)
    except AnswerExtractionError as exc:
        answer = {"_malformed_stdout": stdout[:4096], "_extract_reason": exc.reason}
        verifier = VerifierResult(
            parsed_ok=False,
            raw_metric=0.0,
            rejection_reason="malformed_answer",
            details={"extract_reason": exc.reason},
        )
    else:
        submission = Submission(
            task_id=problem.task_id,
            miner_hotkey=str(submission_row["miner_hotkey"]),
            answer=answer,
        )
        try:
            verifier = _verify_file_backed_synthetic_boolean(problem, hidden, submission)
            if verifier is None:
                verifier = lane.verify(problem, hidden, submission)
        except Exception as exc:
            verifier = VerifierResult(
                parsed_ok=False,
                raw_metric=0.0,
                rejection_reason="verifier_error",
                details={"error": str(exc)[:512]},
            )

    submission = Submission(
        task_id=problem.task_id,
        miner_hotkey=str(submission_row["miner_hotkey"]),
        answer=answer,
    )
    if score_override is not None:
        score = score_override
    elif verifier.rejection_reason == "verifier_error":
        score = ScoreResult(
            weighted_score=0.0,
            rejection_reason="verifier_error",
            score_parts={"binary_correct": 0.0},
        )
    else:
        try:
            score = lane.score(problem, verifier)
        except Exception as exc:
            score = ScoreResult(weighted_score=0.0, rejection_reason="scorer_error")
            verifier = VerifierResult(
                parsed_ok=False,
                raw_metric=0.0,
                rejection_reason="scorer_error",
                details={"error": str(exc)[:512]},
            )

    row = build_signed_task_family_row(
        eval_run_id=eval_run_id or str(uuid4()),
        submission_id=str(submission_row["id"]),
        agent_display_name=str(submission_row.get("display_name") or ""),
        miner_hotkey=str(submission_row["miner_hotkey"]),
        problem=problem,
        submission=submission,
        verifier=verifier,
        score=score,
        ran_at_iso=ran_at_iso,
        signer=signer,
        epoch_salt=epoch_salt,
    )
    return TaskFamilySignedResult(
        row=row,
        verifier=verifier,
        score=score,
        prompt=prompt,
        submission=submission,
    )


def _verify_file_backed_synthetic_boolean(
    problem: PublicProblem,
    hidden: Any,
    submission: Submission,
) -> VerifierResult | None:
    """Publisher-side SAT verification for hidden file-backed CNFs.

    The lane package remains pure and I/O-free. When the publisher's
    challenge source stores only a local CNF path, this helper performs
    the same answer validation and DIMACS result mapping the lane uses,
    but streams the CNF from disk through publisher-owned code.
    """
    if problem.task_family != "synthetic_boolean_v1":
        return None
    hidden_payload = hidden.hidden_payload if isinstance(hidden.hidden_payload, dict) else {}
    cnf_path = hidden_payload["cnf_path"] if "cnf_path" in hidden_payload else None
    if not isinstance(cnf_path, str) or not cnf_path:
        return None

    answer = submission.answer
    if not isinstance(answer, dict):
        return VerifierResult(
            parsed_ok=False,
            raw_metric=0.0,
            rejection_reason="answer_not_object",
            details={},
        )
    raw_solution = answer["dimacs_solution"] if "dimacs_solution" in answer else None
    if not isinstance(raw_solution, str):
        return VerifierResult(
            parsed_ok=False,
            raw_metric=0.0,
            rejection_reason="answer_missing_dimacs_solution",
            details={},
        )
    if set(answer) != {"dimacs_solution"}:
        return VerifierResult(
            parsed_ok=False,
            raw_metric=0.0,
            rejection_reason="answer_unexpected_keys",
            details={"keys": sorted(str(k) for k in answer)},
        )

    from cathedral.publisher.sat_file_verifier import verify_dimacs_solution_file

    verification = verify_dimacs_solution_file(cnf_path, raw_solution)
    if not verification.parsed_ok:
        return VerifierResult(
            parsed_ok=False,
            raw_metric=0.0,
            rejection_reason=verification.rejection_reason or "solution_unparseable",
            details={
                "status": verification.status,
                "clause_count": verification.clause_count,
                "num_vars": verification.num_vars,
                "assigned": verification.assigned,
            },
        )
    if not verification.satisfied:
        return VerifierResult(
            parsed_ok=True,
            raw_metric=0.0,
            rejection_reason=verification.rejection_reason or "solution_unsatisfied",
            details={
                "clauses_satisfied": verification.clauses_satisfied,
                "clause_count": verification.clause_count,
            },
        )
    return VerifierResult(
        parsed_ok=True,
        raw_metric=1.0,
        rejection_reason=None,
        details={
            "clauses_satisfied": verification.clauses_satisfied,
            "clause_count": verification.clause_count,
        },
    )


async def persist_task_family_result(
    conn: aiosqlite.Connection,
    *,
    submission_row: dict[str, Any],
    problem: PublicProblem,
    signed: TaskFamilySignedResult,
    epoch: int,
    round_index: int,
    duration_ms: int,
    trace_json: dict[str, Any] | None = None,
    feed_enabled: bool | None = None,
    commit: bool = True,
) -> None:
    from cathedral.publisher import repository

    if feed_enabled is None:
        feed_enabled = task_family_feed_enabled()
    if not feed_enabled:
        return

    row = signed.row
    output_card_json = {
        "task_type": problem.task_family,
        "task_id_public": row["task_id_public"],
        "difficulty_tier": problem.difficulty_tier,
        "weighted_score": row["weighted_score"],
        "rejection_reason": row.get("rejection_reason"),
        "worker_owner_hotkey": submission_row["miner_hotkey"],
    }
    output_card_hash = blake3.blake3(canonical_json(output_card_json)).hexdigest()
    task_json = {
        "task_type": problem.task_family,
        "task_id_public": row["task_id_public"],
        "epoch_salt": row["epoch_salt"],
        "difficulty_tier": problem.difficulty_tier,
        "time_limit_seconds": problem.time_limit_seconds,
        "answer_hash": row["answer_hash"],
        "verifier_details_hash": row["verifier_details_hash"],
    }
    errors = [str(row["rejection_reason"])] if row.get("rejection_reason") else None

    await repository.insert_eval_run(
        conn,
        id=str(row["id"]),
        submission_id=str(submission_row["id"]),
        epoch=epoch,
        round_index=round_index,
        polaris_agent_id=f"ssh-hermes:{str(submission_row['miner_hotkey'])[:12]}",
        polaris_run_id=f"{problem.task_family}:{row['id']}",
        task_json=task_json,
        output_card_json=output_card_json,
        output_card_hash=output_card_hash,
        score_parts=dict(row["score_parts"]),
        weighted_score=float(row["weighted_score"]),
        ran_at=_parse_ms_iso(str(row["ran_at"])),
        ran_at_iso=str(row["ran_at"]),
        duration_ms=duration_ms,
        errors=errors,
        cathedral_signature=str(row["cathedral_signature"]),
        polaris_verified=False,
        trace_json=trace_json,
        eval_output_schema_version=int(row["eval_output_schema_version"]),
        commit=commit,
    )


def public_problem_hash(problem: PublicProblem, *, epoch_salt: str) -> str:
    return public_task_id(problem.task_id, epoch_salt=epoch_salt)


def _decode_json(blob: str, *, source: str) -> dict[str, Any]:
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise AnswerExtractionError(
            "json_decode_failed", f"{source}: {exc.msg} at pos {exc.pos}"
        ) from exc
    if not isinstance(parsed, dict):
        raise AnswerExtractionError(
            "json_not_object", f"{source} parsed to {type(parsed).__name__}"
        )
    return parsed


def _scan_last_json_object(stdout: str) -> dict[str, Any] | None:
    closes: list[int] = []
    for i, ch in enumerate(stdout):
        if ch == "}":
            closes.append(i)
    while closes:
        end = closes.pop()
        depth = 0
        for j in range(end, -1, -1):
            c = stdout[j]
            if c == "}":
                depth += 1
            elif c == "{":
                depth -= 1
                if depth == 0:
                    candidate = stdout[j : end + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
    return None


def _parse_ms_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


__all__ = [
    "AnswerExtractionError",
    "TaskFamilySignedResult",
    "build_generate_ctx",
    "build_task_family_prompt",
    "enabled_task_family_ids",
    "extract_answer",
    "persist_task_family_result",
    "public_problem_hash",
    "score_and_sign_task_family_stdout",
    "task_family_feed_enabled",
    "task_family_prober_version_warning",
    "task_family_runner_skip_reason",
    "task_family_tier",
]
