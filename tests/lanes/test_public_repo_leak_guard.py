from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral.eval.eval_signer import EvalSigner
from cathedral.lanes.contract import PublicProblem, ScoreResult, Submission, VerifierResult
from cathedral.lanes.sign import build_signed_task_family_row

ROOT = Path(__file__).resolve().parents[2]


# Strings the public surface for a schema-5 boolean row must NEVER contain.
# All of these are private artifacts the publisher holds or the miner
# submitted in the clear. Cathedral's public projection is hash-only.
_FORBIDDEN_PUBLIC_SUBSTRINGS = (
    # raw CNF and DIMACS markers
    "p cnf",
    "s SATISFIABLE",
    # candidate answer-payload keys
    "dimacs_solution",
    "assignment",
    "solution",
    # generator / private filename markers
    "planted_assignment",
    "generator_version",
    "private_corpus",
    "/private/",
    ".cnf",
    ".dimacs",
    ".sol",
    # CNF URL transport: the fetch URL, its integrity hash, and the
    # per-announcement token are operational artifacts. They live in
    # public_input (PublicProblem) at announce time, but must never
    # round-trip through the signed schema-5 row or the public
    # leaderboard projection. A leak here would let a non-eligible
    # observer pull the CNF body straight off the public feed.
    "cnf_url",
    "cnf_sha256",
    "fetch_token",
)

_SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".venv-byo",
    ".venv-fix",
    ".venv-int",
    ".venv-local",
    ".venv-prefix",
    "__pycache__",
    "build",
    "data",
    "dist",
    "htmlcov",
    "node_modules",
    "secrets",
    "venv",
}

_TEXT_SUFFIXES = {
    ".c",
    ".cfg",
    ".cnf",
    ".cpp",
    ".css",
    ".csv",
    ".dimacs",
    ".html",
    ".ini",
    ".json",
    ".jsonl",
    ".md",
    ".py",
    ".sol",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

_MAX_PUBLIC_CNF_BYTES = 10_000


def _private_corpus_markers() -> tuple[str, ...]:
    # Build strings from pieces so this guard does not trip on itself.
    return (
        "sro" + "gatch",
        "fred-" + "bsat",
        "rse" + "rge",
        "uf20-" + "01",
        "uf50-" + "01000",
        "uf250-" + "0100",
        "sha" + "1.cnf",
    )


def _private_sat_artifact_names() -> tuple[str, ...]:
    return (
        "1_" + "uf20-" + "01.cnf",
        "2_" + "uf50-" + "01000.cnf",
        "3_" + "uf250-" + "0100.cnf",
        "4_" + "f" + "600.cnf",
        "5_" + "f" + "1000.cnf",
        "6_" + "f" + "2000.cnf",
        "7_" + "sha" + "1.cnf",
    )


def _walk_repo_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            files.append(path)
    return files


def test_private_sat_artifact_names_are_not_committed() -> None:
    offenders: list[str] = []
    private_names = set(_private_sat_artifact_names())
    for path in _walk_repo_files():
        if path.name in private_names:
            offenders.append(str(path.relative_to(ROOT)))

    assert not offenders, f"private SAT artifact filenames committed: {offenders}"


def test_no_large_public_sat_formula_or_solution_artifacts() -> None:
    offenders: list[str] = []
    for path in _walk_repo_files():
        if path.suffix.lower() not in {".cnf", ".dimacs", ".sol"}:
            continue
        if path.stat().st_size > _MAX_PUBLIC_CNF_BYTES:
            offenders.append(f"{path.relative_to(ROOT)} ({path.stat().st_size} bytes)")

    assert not offenders, (
        "large SAT formula or solution artifacts must stay private, not in "
        f"cathedralai/cathedral: {offenders}"
    )


def test_security_guard_workflow_covers_projection_inputs() -> None:
    """The leak guard CI job must run when projection/signing code changes."""
    workflow = (ROOT / ".github/workflows/task-family-security-guard.yml").read_text(
        encoding="utf-8"
    )
    required_paths = (
        "src/cathedral/eval/ssh_hermes_runner.py",
        "src/cathedral/eval/scoring_pipeline.py",
        "src/cathedral/lanes/**",
        "src/cathedral/publisher/challenge_cnf.py",
        "src/cathedral/publisher/reads.py",
        "tests/lanes/**",
    )
    missing = [path for path in required_paths if path not in workflow]

    assert not missing, f"task-family security guard workflow missing path filters: {missing}"


def test_private_corpus_markers_are_not_committed_in_text_files() -> None:
    offenders: list[str] = []
    markers = _private_corpus_markers()
    for path in _walk_repo_files():
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lowered = text.lower()
        hits = [marker for marker in markers if marker.lower() in lowered]
        if hits:
            offenders.append(f"{path.relative_to(ROOT)}: {hits}")

    assert not offenders, f"private SAT corpus or generator markers committed: {offenders}"


# --------------------------------------------------------------------------
# Public projection guard: schema-5 rows on the wire must be hash-only
# --------------------------------------------------------------------------


def _schema5_signed_row() -> dict[str, object]:
    """Build a schema-5 signed task-family row that carries every
    private-shaped field the publisher could plausibly hold. The
    test then asserts the public projection drops them all."""
    sk = Ed25519PrivateKey.generate()
    problem = PublicProblem(
        task_family="synthetic_boolean_v1",
        schema_version=1,
        # Private task id; the public projection must surface
        # ``task_id_public`` (hashed) and NEVER the raw task_id.
        task_id="private-task-id-leak-canary-001",
        difficulty_tier=2,
        public_input={
            # Raw CNF is a public input from the miner's perspective in
            # later schema versions, but for the schema-5 public-feed
            # projection we still want NO CNF body to surface in the
            # serialized wire row. The projection lives in reads.py and
            # is hash-only.
            "format": "dimacs",
            "cnf": "p cnf 1 1\n1 0\n",
        },
        time_limit_seconds=60,
    )
    submission = Submission(
        task_id=problem.task_id,
        miner_hotkey="5MinerLeakCanary",
        # The miner submitted a dimacs_solution in the clear; the
        # signed row must redact it.
        answer={"dimacs_solution": "s SATISFIABLE\nv 1 0\n"},
    )
    verifier = VerifierResult(
        parsed_ok=True,
        raw_metric=1.0,
        details={"clauses_satisfied": 1, "clause_count": 1},
    )
    score = ScoreResult(weighted_score=1.0, score_parts={"binary_correct": 1.0})
    row = build_signed_task_family_row(
        eval_run_id="run-leak-canary",
        submission_id="submission-leak",
        agent_display_name="Leak Canary",
        miner_hotkey="5MinerLeakCanary",
        problem=problem,
        submission=submission,
        verifier=verifier,
        score=score,
        ran_at_iso="2026-05-18T20:00:00.000Z",
        signer=EvalSigner(sk),
        epoch_salt="epoch_leak_canary:synthetic_boolean_v1",
    )
    return row


def test_schema5_signed_row_redacts_private_artifacts() -> None:
    """The signed wire row produced by build_signed_task_family_row
    must not surface CNF, dimacs_solution, assignment, planted
    metadata, or private filenames."""
    row = _schema5_signed_row()
    serialized = json.dumps(row, sort_keys=True)

    offenders = [s for s in _FORBIDDEN_PUBLIC_SUBSTRINGS if s in serialized]
    assert not offenders, (
        f"private artifact(s) leaked into schema-5 signed row: {offenders}\nrow={row!r}"
    )

    # Positive shape: the public surface MUST carry the hash anchors so
    # validators can re-canonicalize and verify the signature.
    assert "task_id_public" in row
    assert "answer_hash" in row
    assert "verifier_details_hash" in row
    assert "cathedral_signature" in row
    assert row["eval_output_schema_version"] == 5

    # The private task_id must NOT round-trip on the wire.
    assert "private-task-id-leak-canary-001" not in serialized


def test_schema5_public_projection_is_hash_only() -> None:
    """Simulate the publisher's public_feed projection
    (_eval_run_to_output) for a schema-5 row and confirm nothing
    private slips out.

    This is the leak gate behind /v1/leaderboard/recent for schema-5
    Task Family rows when CATHEDRAL_TASK_FAMILY_FEED_ENABLED=true.
    """
    from cathedral.publisher.reads import _eval_run_to_output

    row = _schema5_signed_row()
    # Mirror what the repository row looks like after persist_task_family_result.
    db_row = {
        "id": row["id"],
        "weighted_score": row["weighted_score"],
        "score_parts": dict(row["score_parts"]),
        "ran_at": row["ran_at"],
        "eval_output_schema_version": 5,
        "cathedral_signature": row["cathedral_signature"],
        "merkle_epoch": 501,
        "task_json": {
            # The publisher stores task_id_public, epoch_salt,
            # answer_hash, verifier_details_hash in task_json -- never
            # the raw answer or raw CNF.
            "task_type": "synthetic_boolean_v1",
            "task_id_public": row["task_id_public"],
            "epoch_salt": row["epoch_salt"],
            "difficulty_tier": 2,
            "answer_hash": row["answer_hash"],
            "verifier_details_hash": row["verifier_details_hash"],
        },
        "output_card_json": {
            # Output card is a small public excerpt -- task_type,
            # task_id_public, weighted_score, rejection_reason. No CNF
            # body, no solution, no assignment.
            "task_type": "synthetic_boolean_v1",
            "task_id_public": row["task_id_public"],
            "difficulty_tier": 2,
            "weighted_score": 1.0,
            "rejection_reason": None,
            "worker_owner_hotkey": "5MinerLeakCanary",
        },
    }
    sub_row = {
        "id": "submission-leak",
        "display_name": "Leak Canary",
        "miner_hotkey": "5MinerLeakCanary",
    }

    wire = _eval_run_to_output(db_row, sub_row)
    serialized = json.dumps(wire, sort_keys=True, default=str)

    offenders = [s for s in _FORBIDDEN_PUBLIC_SUBSTRINGS if s in serialized]
    assert not offenders, (
        f"private artifact(s) leaked into schema-5 public projection: {offenders}\nwire={wire!r}"
    )

    # Positive shape: hash anchors + score + signature, nothing else
    # that resembles a raw artifact.
    assert wire["eval_output_schema_version"] == 5
    assert wire["task_id_public"] == row["task_id_public"]
    assert wire["answer_hash"] == row["answer_hash"]
    assert wire["verifier_details_hash"] == row["verifier_details_hash"]
    assert wire["cathedral_signature"] == row["cathedral_signature"]
    assert wire["weighted_score"] == 1.0

    # And no fields shaped like a payload body, or like the operational
    # CNF URL transport (cnf_url / cnf_sha256 / fetch_token belong only
    # on PublicProblem.public_input at announce time).
    forbidden_keys = {
        "cnf",
        "dimacs",
        "dimacs_solution",
        "assignment",
        "solution",
        "planted_assignment",
        "generator_version",
        "public_input",
        "hidden_payload",
        "cnf_url",
        "cnf_sha256",
        "fetch_token",
    }
    leaked_keys = forbidden_keys & set(wire.keys())
    assert not leaked_keys, f"schema-5 public projection exposes payload-shaped keys: {leaked_keys}"


@pytest.mark.parametrize(
    "candidate_field",
    [
        "cnf",
        "dimacs",
        "dimacs_solution",
        "assignment",
        "solution",
        "planted_assignment",
        "cnf_url",
        "cnf_sha256",
        "fetch_token",
    ],
)
def test_schema5_projection_drops_extra_private_fields_if_repo_grows_them(
    candidate_field: str,
) -> None:
    """Defense-in-depth. If a future change starts stuffing a payload
    field into output_card_json or task_json, the public projection
    must NOT promote it to the wire."""
    from cathedral.publisher.reads import _eval_run_to_output

    row = _schema5_signed_row()
    db_row = {
        "id": row["id"],
        "weighted_score": row["weighted_score"],
        "score_parts": dict(row["score_parts"]),
        "ran_at": row["ran_at"],
        "eval_output_schema_version": 5,
        "cathedral_signature": row["cathedral_signature"],
        "merkle_epoch": 501,
        "task_json": {
            "task_type": "synthetic_boolean_v1",
            "task_id_public": row["task_id_public"],
            "epoch_salt": row["epoch_salt"],
            "difficulty_tier": 2,
            "answer_hash": row["answer_hash"],
            "verifier_details_hash": row["verifier_details_hash"],
            # Hostile addition: stuff a private-shaped key into task_json.
            candidate_field: "LEAK_CANARY_SENTINEL_VALUE",
        },
        "output_card_json": {
            "task_type": "synthetic_boolean_v1",
            "task_id_public": row["task_id_public"],
            "weighted_score": 1.0,
            "rejection_reason": None,
            # Same hostile addition on the output side.
            candidate_field: "LEAK_CANARY_SENTINEL_VALUE",
        },
    }
    sub_row = {
        "id": "submission-leak",
        "display_name": "Leak Canary",
        "miner_hotkey": "5MinerLeakCanary",
    }

    wire = _eval_run_to_output(db_row, sub_row)
    assert "LEAK_CANARY_SENTINEL_VALUE" not in json.dumps(wire, default=str), (
        f"public projection forwarded private field {candidate_field!r} to the wire"
    )
    assert candidate_field not in wire, f"public projection exposes {candidate_field!r} key"
