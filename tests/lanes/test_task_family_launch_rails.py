from __future__ import annotations

import importlib.util as ilu
import inspect
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral.eval.eval_signer import EvalSigner
from cathedral.lanes import publisher as publisher_module
from cathedral.lanes.contract import (
    HiddenMetadata,
    PublicProblem,
    ScoreResult,
    Submission,
    VerifierResult,
)
from cathedral.lanes.publisher import (
    AnswerExtractionError,
    build_task_family_prompt,
    extract_answer,
    score_and_sign_task_family_stdout,
    task_family_prober_version_warning,
    task_family_runner_skip_reason,
)
from cathedral.lanes.sign import (
    TASK_FAMILY_SCHEMA_VERSION,
    TASK_FAMILY_SCHEMA_VERSION_V6,
    TASK_FAMILY_SIGNED_KEYS,
    TASK_FAMILY_SIGNED_KEYS_V6,
    build_signed_task_family_row,
    public_task_id,
)
from cathedral.lanes.synthetic_boolean_v1 import SyntheticBooleanV1
from cathedral.validator import pull_loop
from cathedral.validator.db import connect
from cathedral.validator.pull_loop import latest_pulled_score_per_hotkey, upsert_pulled_eval

_ROOT = Path(__file__).resolve().parents[2]


def _load_v2_payload_module():
    name = "cathedral.eval.v2_payload"
    if name in sys.modules and hasattr(sys.modules[name], "_SIGNED_KEYS_BY_VERSION"):
        return sys.modules[name]
    path = _ROOT / "src" / "cathedral" / "eval" / "v2_payload.py"
    spec = ilu.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _problem() -> PublicProblem:
    return PublicProblem(
        task_family="synthetic_boolean_v1",
        schema_version=1,
        task_id="private-task-id-001",
        difficulty_tier=1,
        public_input={"format": "dimacs", "cnf": "p cnf 1 1\n1 0\n"},
        time_limit_seconds=60,
    )


def _signed_row() -> tuple[dict[str, object], Ed25519PrivateKey]:
    sk = Ed25519PrivateKey.generate()
    problem = _problem()
    submission = Submission(
        task_id=problem.task_id,
        miner_hotkey="5Miner",
        answer={"dimacs_solution": "s SATISFIABLE\nv 1 0\n"},
    )
    verifier = VerifierResult(
        parsed_ok=True,
        raw_metric=1.0,
        details={"clauses_satisfied": 1, "clause_count": 1},
    )
    score = ScoreResult(weighted_score=1.0, score_parts={"binary_correct": 1.0})
    row = build_signed_task_family_row(
        eval_run_id="run-task-family-1",
        submission_id="submission-1",
        agent_display_name="Boolean Miner",
        miner_hotkey="5Miner",
        problem=problem,
        submission=submission,
        verifier=verifier,
        score=score,
        ran_at_iso="2026-05-18T20:00:00.000Z",
        signer=EvalSigner(sk),
        epoch_salt="epoch_123:synthetic_boolean_v1",
    )
    return row, sk


def test_task_family_signed_row_verifies_without_raw_problem_or_answer() -> None:
    row, sk = _signed_row()

    assert row["eval_output_schema_version"] == TASK_FAMILY_SCHEMA_VERSION
    assert row["task_id_public"] == public_task_id(
        "private-task-id-001",
        epoch_salt="epoch_123:synthetic_boolean_v1",
    )
    assert "private-task-id-001" not in str(row)
    # No raw answer key leaks into the signed wire row.
    assert "dimacs_solution" not in str(row)
    assert "SATISFIABLE" not in str(row)
    assert "assignment" not in str(row)

    pull_loop.verify_eval_output_signature(row, sk.public_key())


def test_task_family_keysets_match_publisher_and_validator() -> None:
    publisher_keys = _load_v2_payload_module()._SIGNED_KEYS_BY_VERSION
    assert publisher_keys[TASK_FAMILY_SCHEMA_VERSION] == TASK_FAMILY_SIGNED_KEYS
    assert pull_loop._SIGNED_KEYS_BY_VERSION[TASK_FAMILY_SCHEMA_VERSION] == (
        TASK_FAMILY_SIGNED_KEYS
    )


def _signed_row_v6() -> tuple[dict[str, object], Ed25519PrivateKey]:
    sk = Ed25519PrivateKey.generate()
    problem = _problem()
    submission = Submission(
        task_id=problem.task_id,
        miner_hotkey="5Miner",
        answer={"dimacs_solution": "s SATISFIABLE\nv 1 0\n"},
    )
    verifier = VerifierResult(
        parsed_ok=True, raw_metric=1.0, details={"clauses_satisfied": 1, "clause_count": 1}
    )
    score = ScoreResult(weighted_score=1.0, score_parts={"binary_correct": 1.0})
    row = build_signed_task_family_row(
        eval_run_id="run-v6-1",
        submission_id="submission-1",
        agent_display_name="Boolean Miner",
        miner_hotkey="5Miner",
        problem=problem,
        submission=submission,
        verifier=verifier,
        score=score,
        ran_at_iso="2026-05-29T20:00:00.000Z",
        signer=EvalSigner(sk),
        epoch_salt="epoch_123:synthetic_boolean_v1",
        schema_version=TASK_FAMILY_SCHEMA_VERSION_V6,
        challenge_value=3.0,
        solve_rank=2,
        solved=True,
        operator="5ColdkeyOperator",
    )
    return row, sk


def test_task_family_v6_keysets_match_across_modules() -> None:
    pub = _load_v2_payload_module()._SIGNED_KEYS_BY_VERSION
    assert pub[TASK_FAMILY_SCHEMA_VERSION_V6] == TASK_FAMILY_SIGNED_KEYS_V6
    assert pull_loop._SIGNED_KEYS_BY_VERSION[TASK_FAMILY_SCHEMA_VERSION_V6] == (
        TASK_FAMILY_SIGNED_KEYS_V6
    )


def test_task_family_v6_row_verifies_and_carries_par2_facts() -> None:
    row, sk = _signed_row_v6()
    assert row["eval_output_schema_version"] == TASK_FAMILY_SCHEMA_VERSION_V6
    assert row["challenge_value"] == 3.0
    assert row["solve_rank"] == 2
    assert row["solved"] is True
    assert row["operator"] == "5ColdkeyOperator"
    assert 0.0 <= float(row["weighted_score"]) <= 1.0  # magnitude stays in [0,1]
    pull_loop.verify_eval_output_signature(row, sk.public_key())


def test_task_family_v5_default_is_unchanged() -> None:
    # No schema_version arg -> v5, byte-identical legacy shape; v6 fields absent.
    row, sk = _signed_row()
    assert row["eval_output_schema_version"] == TASK_FAMILY_SCHEMA_VERSION
    assert "challenge_value" not in row
    assert "operator" not in row
    pull_loop.verify_eval_output_signature(row, sk.public_key())


def test_task_family_signed_row_rejects_tampered_score() -> None:
    row, sk = _signed_row()
    row["weighted_score"] = 0.0

    with pytest.raises(pull_loop.PullVerificationError):
        pull_loop.verify_eval_output_signature(row, sk.public_key())


def test_task_family_zero_all_scores_killswitch_forces_signed_zero(monkeypatch) -> None:
    sk = Ed25519PrivateKey.generate()
    monkeypatch.setenv("CATHEDRAL_ZERO_ALL_SCORES", "true")

    signed = score_and_sign_task_family_stdout(
        lane=SyntheticBooleanV1(),
        problem=_problem(),
        hidden=HiddenMetadata(
            task_id="private-task-id-001",
            generator_version="test",
            hidden_payload={"cnf": "p cnf 1 1\n1 0\n"},
        ),
        submission_row={
            "id": "submission-killswitch",
            "miner_hotkey": "5Miner",
            "display_name": "Boolean Miner",
        },
        stdout='```FINAL_ANSWER\n{"dimacs_solution": "s SATISFIABLE\\nv 1 0\\n"}\n```',
        ran_at_iso="2026-05-18T20:00:00.000Z",
        signer=EvalSigner(sk),
        eval_run_id="run-killswitch",
        epoch_salt="epoch_123:synthetic_boolean_v1",
    )

    assert signed.score.weighted_score == 0.0
    assert signed.row["weighted_score"] == 0.0
    assert signed.row["rejection_reason"] == "CATHEDRAL_ZERO_ALL_SCORES=true"
    assert signed.row["score_parts"] == {"binary_correct": 0.0}
    pull_loop.verify_eval_output_signature(signed.row, sk.public_key())


def test_task_family_answer_extraction_prefers_final_answer_block() -> None:
    stdout = """notes
```FINAL_ANSWER
{"dimacs_solution": "s SATISFIABLE\\nv 1 0\\n"}
```
"""
    assert extract_answer(stdout) == {"dimacs_solution": "s SATISFIABLE\nv 1 0\n"}


def test_task_family_answer_extraction_rejects_multiple_final_answer_blocks() -> None:
    stdout = """```FINAL_ANSWER
{"dimacs_solution": "s SATISFIABLE\\nv 1 0\\n"}
```
```FINAL_ANSWER
{"dimacs_solution": "s SATISFIABLE\\nv -1 0\\n"}
```
"""
    with pytest.raises(AnswerExtractionError) as exc:
        extract_answer(stdout)
    assert exc.value.reason == "multiple_final_answer_blocks"


def test_task_family_answer_extraction_fallback_is_linear(monkeypatch: pytest.MonkeyPatch) -> None:
    real_loads = publisher_module.json.loads
    loads_calls = 0

    def counting_loads(blob: str):
        nonlocal loads_calls
        loads_calls += 1
        return real_loads(blob)

    monkeypatch.setattr(publisher_module.json, "loads", counting_loads)

    stdout = (
        "miner log without a fence\n"
        + ("}" * 100_000)
        + '\n{"dimacs_solution": "s SATISFIABLE\\nv 1 0\\n"}'
    )

    assert extract_answer(stdout) == {"dimacs_solution": "s SATISFIABLE\nv 1 0\n"}
    assert loads_calls == 1


def test_task_family_answer_extraction_recovers_after_unmatched_log_brace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_loads = publisher_module.json.loads
    loads_calls = 0

    def counting_loads(blob: str):
        nonlocal loads_calls
        loads_calls += 1
        return real_loads(blob)

    monkeypatch.setattr(publisher_module.json, "loads", counting_loads)

    stdout = (
        "debug: solver emitted an unmatched brace here { still logging\n"
        "summary follows\n"
        '{"dimacs_solution": "s SATISFIABLE\\nv 1 0\\n"}'
    )

    assert extract_answer(stdout) == {"dimacs_solution": "s SATISFIABLE\nv 1 0\n"}
    assert loads_calls == 1


def test_task_family_answer_extraction_accepts_labeled_bare_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_loads = publisher_module.json.loads
    loads_calls = 0

    def counting_loads(blob: str):
        nonlocal loads_calls
        loads_calls += 1
        return real_loads(blob)

    monkeypatch.setattr(publisher_module.json, "loads", counting_loads)

    stdout = 'Final answer: {"dimacs_solution": "s SATISFIABLE\\nv 1 0\\n"}'

    assert extract_answer(stdout) == {"dimacs_solution": "s SATISFIABLE\nv 1 0\n"}
    assert loads_calls == 1


def test_task_family_answer_extraction_accepts_labeled_json_after_unmatched_brace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_loads = publisher_module.json.loads
    loads_calls = 0

    def counting_loads(blob: str):
        nonlocal loads_calls
        loads_calls += 1
        return real_loads(blob)

    monkeypatch.setattr(publisher_module.json, "loads", counting_loads)

    stdout = (
        'debug: solver emitted a partial object here {"event": "still logging"\n'
        'Final answer: {"dimacs_solution": "s SATISFIABLE\\nv 1 0\\n"}'
    )

    assert extract_answer(stdout) == {"dimacs_solution": "s SATISFIABLE\nv 1 0\n"}
    assert loads_calls == 1


def test_task_family_prompt_keeps_challenge_generic() -> None:
    prompt = build_task_family_prompt(_problem())

    assert "Capability: synthetic_boolean_v1" in prompt
    assert "FINAL_ANSWER" in prompt
    assert "p cnf 1 1" in prompt


def test_task_family_prompt_omits_cnf_body_when_url_transport_in_use() -> None:
    """When the publisher uses the CNF URL transport, ``public_input``
    carries ``cnf_url`` + ``cnf_sha256`` instead of the inline ``cnf``.
    The prompt must not contain the CNF body in that case -- the body
    crosses the wire via the gated endpoint, not the prompt."""
    url_problem = PublicProblem(
        task_family="synthetic_boolean_v1",
        schema_version=1,
        task_id="url-task-id-001",
        difficulty_tier=1,
        public_input={
            "format": "dimacs",
            "cnf_url": "https://api.cathedral.test/v1/challenges/sat-x/cnf?t=opaque",
            "cnf_sha256": "0" * 64,
            "num_vars": 3,
            "num_clauses": 2,
        },
        time_limit_seconds=60,
    )
    prompt = build_task_family_prompt(url_problem)

    assert "Capability: synthetic_boolean_v1" in prompt
    assert "cnf_url" in prompt
    assert "cnf_sha256" in prompt
    # The DIMACS marker for an inline body must not appear anywhere
    # in the rendered prompt -- the URL transport carries the URL, not
    # the body.
    assert "p cnf" not in prompt
    # Miner contract directives the prompt must convey under the URL
    # transport: plain HTTP GET, sha256 verify, no retry-storm on 404,
    # no logging of the token-bearing URL.
    assert "plain HTTP GET" in prompt
    assert "404" in prompt
    assert "never log the URL" in prompt


def test_task_family_runner_guard_names_required_transport_interface() -> None:
    class UnsupportedRunner:
        pass

    skip = task_family_runner_skip_reason(UnsupportedRunner())
    assert skip is not None
    assert skip["reason"] == "runner_unsupported"
    assert skip["required_runner_interface"] == "SshHermesRunner.run_task_family_challenge"
    assert "CATHEDRAL_PROBER_VERSION=v2" in skip["recommended_env"]


def test_task_family_prober_version_warning_is_explicit() -> None:
    warning = task_family_prober_version_warning(
        {
            "CATHEDRAL_TASK_FAMILY_FEED_ENABLED": "true",
            "CATHEDRAL_PROBER_VERSION": "v1",
        }
    )
    assert warning == {
        "reason": "prober_version_not_v2",
        "recommended_env": "CATHEDRAL_PROBER_VERSION=v2",
    }
    assert (
        task_family_prober_version_warning(
            {
                "CATHEDRAL_TASK_FAMILY_FEED_ENABLED": "true",
                "CATHEDRAL_PROBER_VERSION": "v2",
            }
        )
        is None
    )
    assert (
        task_family_prober_version_warning(
            {
                "CATHEDRAL_TASK_FAMILY_FEED_ENABLED": " true ",
                "CATHEDRAL_PROBER_VERSION": "v2 ",
            }
        )
        is None
    )


def test_ssh_hermes_task_family_runner_interface_is_launch_smoked() -> None:
    from cathedral.eval.ssh_hermes_runner import SshHermesRunner

    method = SshHermesRunner.run_task_family_challenge
    assert inspect.iscoroutinefunction(method)
    signature = inspect.signature(method)
    assert list(signature.parameters) == [
        "self",
        "problem",
        "prompt",
        "miner_hotkey",
        "submission",
        "receipt_callback",
    ]
    assert signature.parameters["receipt_callback"].default is None


def test_ssh_hermes_redacts_cnf_fetch_tokens_from_errors() -> None:
    from cathedral.eval.ssh_hermes_runner import _redact_query_tokens

    msg = (
        "cmd='hermes chat -q https://api.cathedral.test/v1/challenges/sat-x/cnf"
        "?t=secret-token-123' stderr='retry https://host/cnf?t=another-token&x=1'"
    )

    redacted = _redact_query_tokens(msg)
    assert "secret-token-123" not in redacted
    assert "another-token" not in redacted
    assert "?t=REDACTED" in redacted


@pytest.mark.asyncio
async def test_synthetic_boolean_weight_defaults_off_and_blends_when_enabled(tmp_path) -> None:
    conn = await connect(str(tmp_path / "validator.db"))
    try:
        # ran_at must stay inside latest_pulled_score_per_hotkey's
        # since_days=7 window; using a fixed literal made this a
        # time-bomb test that started failing once main aged past the
        # original 2026-05-18 fixture date.
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        now = _dt.now(_UTC).isoformat()
        # Legacy card row: post-strip this no longer earns incentive.
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-v1",
                "card_id": "eu-ai-act",
                "weighted_score": 0.80,
                "ran_at": now,
            },
            miner_hotkey="hk-mixed",
        )
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-boolean",
                "task_type": "synthetic_boolean_v1",
                "eval_output_schema_version": TASK_FAMILY_SCHEMA_VERSION,
                "weighted_score": 1.0,
                "ran_at": now,
            },
            miner_hotkey="hk-mixed",
        )
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-boolean-only",
                "task_type": "synthetic_boolean_v1",
                "eval_output_schema_version": TASK_FAMILY_SCHEMA_VERSION,
                "weighted_score": 1.0,
                "ran_at": now,
            },
            miner_hotkey="hk-boolean-only",
        )

        # SAT lane weight off -> nobody scores (the legacy card row is
        # dropped, not bucketed as v1).
        disabled = await latest_pulled_score_per_hotkey(
            conn,
            since_days=7,
            task_family_weights={"synthetic_boolean_v1": 0.0},
        )
        assert "hk-mixed" not in disabled
        assert "hk-boolean-only" not in disabled

        enabled = await latest_pulled_score_per_hotkey(
            conn,
            since_days=7,
            task_family_weights={"synthetic_boolean_v1": 0.05},
        )
        assert enabled["hk-mixed"] == pytest.approx(0.05)
        assert enabled["hk-boolean-only"] == pytest.approx(0.05)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_unknown_schema5_task_family_contributes_zero(tmp_path) -> None:
    """A schema-5 row whose task_type is not in the configured
    ``task_family_weights`` map must contribute zero to the miner's score.

    Regression for the schema-5 weighting blocker: previously an
    unconfigured task family would fall through to the v1 bucket and
    silently award v1-share emissions to a brand-new lane that the
    validator had not yet opted in to. Post card-strip there is no v1
    bucket at all, so both the unknown family AND legacy card rows score
    zero.
    """
    conn = await connect(str(tmp_path / "validator.db"))
    try:
        # ran_at must stay inside the since_days=7 window; same
        # time-bomb fix as the boolean-weight test above.
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        now = _dt.now(_UTC).isoformat()
        # Hotkey has only a schema-5 row from an unknown family --
        # nothing else to fall back to.
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-future-family",
                "task_type": "future_family_v1",
                "eval_output_schema_version": TASK_FAMILY_SCHEMA_VERSION,
                "weighted_score": 1.0,
                "ran_at": now,
            },
            miner_hotkey="hk-future-only",
        )
        # Mixed hotkey: has an unconfigured schema-5 row AND a legacy
        # card row. Neither contributes post-strip.
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-future-family-mixed",
                "task_type": "future_family_v1",
                "eval_output_schema_version": TASK_FAMILY_SCHEMA_VERSION,
                "weighted_score": 1.0,
                "ran_at": now,
            },
            miner_hotkey="hk-mixed-future",
        )
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-v1-baseline",
                "card_id": "eu-ai-act",
                "weighted_score": 0.50,
                "ran_at": now,
            },
            miner_hotkey="hk-mixed-future",
        )

        scores = await latest_pulled_score_per_hotkey(
            conn,
            since_days=7,
            # No weight configured for future_family_v1.
            task_family_weights={"synthetic_boolean_v1": 0.05},
        )

        # hk-future-only had only the unknown-family row -> nothing.
        assert "hk-future-only" not in scores, (
            f"unknown schema-5 family must not score; got {scores.get('hk-future-only')!r}"
        )
        # hk-mixed-future has only an unknown-family row + a legacy card
        # row; both score zero, so the hotkey drops out entirely.
        assert "hk-mixed-future" not in scores, (
            f"unknown schema-5 family + legacy card must not score; got "
            f"{scores.get('hk-mixed-future')!r}"
        )
    finally:
        await conn.close()


# --------------------------------------------------------------------------
# Read-rollback gate for the public feed
# --------------------------------------------------------------------------


async def _seed_v1_card_and_submission(conn) -> dict:
    """Minimal v1-side seed so eval_runs has a submission to join."""
    from cathedral.publisher import repository as repo

    await repo.insert_card_definition(
        conn,
        id="eu-ai-act",
        display_name="EU AI Act",
        jurisdiction="EU",
        topic="AI Act",
        description="primary v1 card",
        eval_spec_md="spec",
        source_pool=[],
        task_templates=[],
        scoring_rubric={},
    )
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    submitted_at = _dt(2026, 5, 18, 19, 0, 0, tzinfo=_UTC)
    await repo.insert_agent_submission(
        conn,
        id="sub-boolean-rails",
        miner_hotkey="5MinerRails",
        card_id="eu-ai-act",
        bundle_blob_key="bundles/sub-boolean-rails.zip",
        bundle_hash="0" * 64,
        bundle_size_bytes=1024,
        encryption_key_id="kek-test",
        bundle_signature="b64:stub",
        display_name="Boolean Miner",
        bio=None,
        logo_url=None,
        soul_md_preview=None,
        metadata_fingerprint="fp-boolean",
        similarity_check_passed=True,
        rejection_reason=None,
        status="ranked",
        submitted_at=submitted_at,
        submitted_at_iso="2026-05-18T19:00:00.000Z",
        first_mover_at=None,
        attestation_mode="ssh-probe",
        discovery_only=False,
        ssh_host="203.0.113.10",
        ssh_port=22,
        ssh_user="cathedral",
    )
    seeded = await repo.get_agent_submission(conn, "sub-boolean-rails")
    assert seeded is not None
    return seeded


@pytest.mark.asyncio
async def test_read_rollback_gate_excludes_schema5_when_flag_off(tmp_path) -> None:
    """``include_task_families=False`` must keep schema-5 (Task Family)
    rows out of the public read surface, the same way ``include_v3=False``
    keeps v3 bug-isolation rows out.

    This is the rollback gate behind ``CATHEDRAL_TASK_FAMILY_FEED_ENABLED``:
    when the flag is off, ``/v1/leaderboard/recent`` must not surface
    schema-5 rows. When the flag is on, they may appear.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from cathedral.publisher import repository as repo

    # validator.db.connect runs the publisher-side schema migrations
    # in tests; same pattern used by tests/v3/test_publisher_bug_isolation.py.
    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        submission = await _seed_v1_card_and_submission(conn)

        # Insert one schema-5 row (Task Family) via the repository insert
        # used by persist_task_family_result.
        ran_at = _dt(2026, 5, 18, 20, 0, 0, tzinfo=_UTC)
        await repo.insert_eval_run(
            conn,
            id="00000000-0000-4000-8000-000000000501",
            submission_id=str(submission["id"]),
            epoch=501,
            round_index=0,
            polaris_agent_id="ssh-hermes:5MinerRails",
            polaris_run_id="synthetic_boolean_v1:run-501",
            task_json={
                "task_type": "synthetic_boolean_v1",
                "task_id_public": "deadbeef",
            },
            output_card_json={
                "task_type": "synthetic_boolean_v1",
                "task_id_public": "deadbeef",
                "weighted_score": 1.0,
            },
            output_card_hash="a" * 64,
            score_parts={"binary_correct": 1.0},
            weighted_score=1.0,
            ran_at=ran_at,
            ran_at_iso="2026-05-18T20:00:00.000Z",
            duration_ms=42,
            errors=None,
            cathedral_signature="b64:stub",
            polaris_verified=False,
            trace_json=None,
            eval_output_schema_version=5,
        )

        since = _dt(2000, 1, 1, tzinfo=_UTC)

        # Flag off: schema-5 row hidden.
        gated = await repo.list_eval_runs_recent(
            conn,
            since=since,
            include_v3=False,
            include_task_families=False,
        )
        assert all(r.get("eval_output_schema_version") != 5 for r in gated), (
            f"schema-5 row leaked into gated read: {gated!r}"
        )

        # Flag on: schema-5 row visible.
        with_families = await repo.list_eval_runs_recent(
            conn,
            since=since,
            include_v3=False,
            include_task_families=True,
        )
        schema5_rows = [r for r in with_families if r.get("eval_output_schema_version") == 5]
        assert len(schema5_rows) == 1, (
            "expected exactly one schema-5 row when include_task_families=True, "
            f"got {with_families!r}"
        )
    finally:
        await conn.close()
