"""Publisher-side bug_isolation_v1 scoring and persistence."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import aiosqlite
import blake3

from cathedral.storage import HippiusClient
from cathedral.v1_types import canonical_json
from cathedral.v3.corpus.schema import ChallengeRow
from cathedral.v3.dispatch import DispatchResult, dispatch_bug_isolation_claim
from cathedral.v3.prompts import build_bug_isolation_prompt
from cathedral.v3.sign import build_signed_v3_bug_isolation_row


def v3_feed_enabled(env: dict[str, str] | None = None) -> bool:
    """True when v3 rows may be written and served."""
    values = os.environ if env is None else env
    return values.get("CATHEDRAL_V3_FEED_ENABLED", "").lower() == "true"


@dataclass(frozen=True)
class BugIsolationSignedResult:
    row: dict[str, Any]
    dispatch: DispatchResult
    prompt: str


def score_and_sign_bug_isolation_stdout(
    *,
    challenge: ChallengeRow,
    submission: dict[str, Any],
    stdout: str,
    ran_at_iso: str,
    signer: Any,
    eval_run_id: str | None = None,
    repair_stdout: str | None = None,
    epoch_salt: str | None = None,
) -> BugIsolationSignedResult:
    """Parse Hermes stdout, score it, and build a signed v3 row."""
    challenge_id = challenge.public_view()["challenge_id"]
    dispatch = dispatch_bug_isolation_claim(
        expected_challenge_id=challenge_id,
        oracle_culprit_file=challenge.culprit_file,
        oracle_culprit_symbol=challenge.culprit_symbol,
        oracle_line_range=challenge.line_range,
        oracle_required_keywords=challenge.required_failure_keywords,
        stdout=stdout,
        repair_stdout=repair_stdout,
    )
    row = build_signed_v3_bug_isolation_row(
        eval_run_id=eval_run_id or str(uuid4()),
        submission_id=str(submission["id"]),
        agent_display_name=str(submission.get("display_name") or ""),
        miner_hotkey=str(submission["miner_hotkey"]),
        challenge_id=challenge_id,
        dispatch_result=dispatch,
        ran_at_iso=ran_at_iso,
        signer=signer,
        failure_reason=dispatch.failure_reason,
        epoch_salt=epoch_salt,
    )
    return BugIsolationSignedResult(
        row=row,
        dispatch=dispatch,
        prompt=build_bug_isolation_prompt(challenge),
    )


async def persist_bug_isolation_result(
    conn: aiosqlite.Connection,
    *,
    submission: dict[str, Any],
    challenge: ChallengeRow,
    signed: BugIsolationSignedResult,
    epoch: int,
    round_index: int,
    duration_ms: int,
    trace_json: dict[str, Any] | None = None,
    feed_enabled: bool | None = None,
) -> None:
    """Persist a signed bug_isolation_v1 row.

    The feed flag gates writes here. This keeps disabled v3 probes from
    leaking into validator pulls or public feeds.
    """
    if feed_enabled is None:
        feed_enabled = v3_feed_enabled()
    if not feed_enabled:
        return

    row = signed.row
    output_card_json = {
        "task_type": "bug_isolation_v1",
        "challenge_id_public": row.get("challenge_id_public"),
        "claim": row.get("claim"),
        "failure_reason": row.get("failure_reason"),
        "worker_owner_hotkey": submission["miner_hotkey"],
    }
    output_card_hash = blake3.blake3(canonical_json(output_card_json)).hexdigest()
    task_json = {
        "task_type": "bug_isolation_v1",
        "challenge_id": row["challenge_id"],
        "challenge_id_public": row.get("challenge_id_public"),
        # epoch_salt is part of the signed subset so the validator
        # must see it on the wire to re-canonicalize correctly. Stash
        # it in task_json so _eval_run_to_output can surface it back.
        "epoch_salt": row.get("epoch_salt"),
        "challenge": challenge.public_view(),
        "prompt": signed.prompt,
    }
    errors = [str(row["failure_reason"])] if row.get("failure_reason") else None
    from cathedral.publisher import repository

    await repository.insert_eval_run(
        conn,
        id=str(row["id"]),
        submission_id=str(submission["id"]),
        epoch=epoch,
        round_index=round_index,
        polaris_agent_id=f"ssh-hermes:{str(submission['miner_hotkey'])[:12]}",
        polaris_run_id=f"bug-isolation:{row['id']}",
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
        eval_output_schema_version=3,
    )


def _parse_ms_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


async def publish_score_sidecar(
    *,
    hippius: HippiusClient,
    eval_id: str,
    score_record: dict[str, Any],
) -> str:
    """Upload the v3 score record as a private JSON sidecar.

    Sibling to the encrypted Hermes bundle under the same
    ``eval-artifacts/`` prefix. Returns the ``s3://`` URI so the caller
    can stash it in the operator-only trace_json (never the public feed).
    """
    client = hippius._ensure_client()
    bucket = hippius.config.bucket
    env_prefix = hippius.config.env_prefix
    key = f"{env_prefix}eval-artifacts/{eval_id}.score_record.json"
    body = canonical_json(score_record)

    def _put() -> str:
        client.put_object(  # type: ignore[attr-defined]
            Bucket=bucket,
            Key=key,
            Body=body,
            Metadata={
                "cathedral-version": "v1.1.0",
                "artifact-kind": "v3-score-record",
            },
            ContentType="application/json",
        )
        return f"s3://{bucket}/{key}"

    return await asyncio.to_thread(_put)


__all__ = [
    "BugIsolationSignedResult",
    "persist_bug_isolation_result",
    "publish_score_sidecar",
    "score_and_sign_bug_isolation_stdout",
    "v3_feed_enabled",
]
