"""End-to-end engine tests: scramble -> build bundle -> verify -> package."""

from __future__ import annotations

import json
import re

from cathedral.v4 import ValidationPayload
from cathedral.v4.cathedral_engine import (
    SIPHON_FLAG_ONE_SHOT,
    CathedralEngine,
)
from cathedral.v4.oracle.patch_runner import (
    BOOKKEEPING_BUDGET_SECONDS,
    REPRO_BUDGET_SECONDS,
)


def _render_template(template: str, rename_map: dict[str, str]) -> str:
    """Replace {{rename:<orig>}} markers with the scrambled identifier."""

    def sub(match: re.Match[str]) -> str:
        original = match.group(1)
        return rename_map.get(original, original)

    return re.sub(r"\{\{rename:([A-Za-z_][A-Za-z0-9_]*)\}\}", sub, template)


def test_load_and_scramble_task_shape(engine: CathedralEngine) -> None:
    task = engine.load_and_scramble_task("python_fastapi_base")
    assert task["base_repo"] == "python_fastapi_base"
    assert task["task_id"].startswith("v4t_")
    assert task["language"] == "python"
    assert isinstance(task["original_repo_state"], dict)
    assert "app/calculator.py" in task["original_repo_state"]
    assert task["arena"] is not None


def test_build_miner_bundle_applies_bug_server_side(
    engine: CathedralEngine, python_manifest: dict
) -> None:
    """The bundle the miner sees must be the BROKEN state.

    Critically, the raw bug patch must NOT appear in the returned
    bundle; only the post-bug-apply file content is shipped. The
    publisher-internal clean_state lives on ``PublisherHandle``,
    NOT on ``MinerBundle``.
    """
    # Build a synthetic bug patch: change the working `apply_tax`
    # function to subtract instead of add. (Independent of the
    # vault's shipped fault so we can prove the apply happens.)
    seed = 999
    scrambled = engine._scrambler.scramble(
        "python_fastapi_base", seed=seed, workspace_root=engine._workspace_root
    )
    apply_tax_scrambled = scrambled.rename_map.get("apply_tax", "apply_tax")
    src = scrambled.files["app/calculator.py"]
    lines = src.splitlines()
    target_idx = next(i for i, ln in enumerate(lines) if "amount + amount * tax_rate" in ln)
    bug_patch = (
        "--- a/app/calculator.py\n"
        "+++ b/app/calculator.py\n"
        f"@@ -{target_idx + 1},1 +{target_idx + 1},1 @@\n"
        f"-    return amount + amount * tax_rate\n"
        f"+    return amount - amount * tax_rate\n"
    )

    bundle, handle = engine.build_bundle_and_handle(
        "python_fastapi_base", bug_patch=bug_patch, seed=seed
    )
    # The broken state reflects the applied bug.
    broken_src = bundle.workspace_files["app/calculator.py"]
    assert "amount - amount * tax_rate" in broken_src
    assert "amount + amount * tax_rate" not in broken_src
    # Clean state is preserved separately on the publisher handle,
    # NOT in the miner-facing bundle.
    clean_src = handle.clean_state["app/calculator.py"]
    assert "amount + amount * tax_rate" in clean_src

    # Hard-block: the bundle JSON must not carry the pre-bug clean
    # state nor the rename map (both would leak the answer).
    bundle_json = bundle.model_dump_json()
    assert "amount + amount * tax_rate" not in bundle_json, (
        "MinerBundle JSON leaked the clean (pre-bug) expression; "
        "clean_state must stay on PublisherHandle"
    )
    assert "clean_state" not in bundle_json
    assert "rename_map" not in bundle_json
    # And literally: the bug patch header must not be in any value.
    assert "--- a/app/calculator.py\\n+++ b/app/calculator.py\\n@@" not in bundle_json
    # Use of `apply_tax_scrambled` keeps the linter happy.
    _ = apply_tax_scrambled


def test_full_round_trip_with_real_winning_patch(
    engine: CathedralEngine, python_manifest: dict
) -> None:
    task = engine.load_and_scramble_task("python_fastapi_base")
    rename_map = task["rename_map"]

    winning_patch = _render_template(python_manifest["winning_patch_template"], rename_map)
    hidden_test = _render_template(python_manifest["hidden_test_template"], rename_map)

    passed, duration = engine.verify_miner_submission(
        original_repo_state=task["original_repo_state"],
        patch_str=winning_patch,
        hidden_test_code=hidden_test,
    )
    assert passed is True, f"winning patch failed in {duration * 1000:.1f}ms"
    # Real subprocess execution under the 3s repro budget.
    assert duration < REPRO_BUDGET_SECONDS


def test_wrong_patch_fails_verification(engine: CathedralEngine, python_manifest: dict) -> None:
    task = engine.load_and_scramble_task("python_fastapi_base")
    rename_map = task["rename_map"]
    hidden_test = _render_template(python_manifest["hidden_test_template"], rename_map)

    bogus_patch = (
        "--- a/app/calculator.py\n"
        "+++ b/app/calculator.py\n"
        "@@ -16,3 +16,3 @@\n"
        "     factor = percent / 100.0\n"
        "     # INJECTED FAULT: should be `price - price * factor`\n"
        "-    return price + price * factor\n"
        "+    return price * price * factor\n"
    )
    passed, duration = engine.verify_miner_submission(
        original_repo_state=task["original_repo_state"],
        patch_str=bogus_patch,
        hidden_test_code=hidden_test,
    )
    assert passed is False
    assert duration < REPRO_BUDGET_SECONDS


def test_package_elite_telemetry_round_trip(engine: CathedralEngine) -> None:
    raw = {
        "task_id": "v4t_abc",
        "difficulty_tier": "bronze",
        "language": "python",
        "injected_fault_type": "sign_error_off_by_operator",
        "winning_patch": "--- a/x\n+++ b/x\n",
        "trajectories": [
            {
                "miner_hotkey": "miner_a",
                "model_identifier": "echo-v1",
                "total_turns": 4,
                "outcome": "SUCCESS",
                "trace": [
                    {
                        "turn_index": 0,
                        "tool_called": "read_file",
                        "arguments": {"path": "app/calculator.py"},
                        "system_response": "def...",
                        "duration_ms": 3,
                    },
                    {
                        "turn_index": 1,
                        "tool_called": "read_file",
                        "arguments": {"path": "app/main.py"},
                        "system_response": "from app...",
                        "duration_ms": 2,
                    },
                    {
                        "turn_index": 2,
                        "tool_called": "write_patch",
                        "arguments": {"diff_string": "--- a/x\n+++ b/x\n"},
                        "system_response": "True",
                        "duration_ms": 5,
                    },
                    {
                        "turn_index": 3,
                        "tool_called": "run_local_compile",
                        "arguments": {},
                        "system_response": "exit 0",
                        "duration_ms": 110,
                    },
                ],
            }
        ],
    }
    out = engine.package_elite_telemetry(raw)
    payload = ValidationPayload.model_validate_json(out)
    assert payload.task_id == "v4t_abc"
    assert len(payload.trajectories) == 1
    assert payload.deterministic_hash != ""
    out2 = engine.package_elite_telemetry(raw)
    payload2 = ValidationPayload.model_validate_json(out2)
    assert payload.deterministic_hash == payload2.deterministic_hash
    assert engine.siphon_flags_for(payload) == {}


def test_siphon_flags_one_shot_trajectory(engine: CathedralEngine) -> None:
    raw = {
        "task_id": "v4t_xyz",
        "difficulty_tier": "bronze",
        "language": "python",
        "injected_fault_type": "sign_error_off_by_operator",
        "winning_patch": "",
        "trajectories": [
            {
                "miner_hotkey": "miner_one_shot",
                "model_identifier": "suspicious-v1",
                "total_turns": 1,
                "outcome": "SUCCESS",
                "trace": [
                    {
                        "turn_index": 0,
                        "tool_called": "write_patch",
                        "arguments": {"diff_string": "--- a/x\n+++ b/x\n"},
                        "system_response": "True",
                        "duration_ms": 7,
                    }
                ],
            },
            {
                "miner_hotkey": "miner_legit",
                "model_identifier": "honest-v1",
                "total_turns": 5,
                "outcome": "SUCCESS",
                "trace": [],
            },
            {
                "miner_hotkey": "miner_failed_quickly",
                "model_identifier": "broken-v1",
                "total_turns": 1,
                "outcome": "FAILURE",
                "trace": [],
            },
        ],
    }
    out = engine.package_elite_telemetry(raw)
    payload = ValidationPayload.model_validate_json(out)
    flags = engine.siphon_flags_for(payload)
    assert "miner_one_shot" in flags
    assert SIPHON_FLAG_ONE_SHOT in flags["miner_one_shot"]
    assert "miner_legit" not in flags
    assert "miner_failed_quickly" not in flags


def test_telemetry_payload_canonical_json(engine: CathedralEngine) -> None:
    raw = {
        "task_id": "v4t_canonical",
        "difficulty_tier": "bronze",
        "language": "python",
        "injected_fault_type": "x",
        "winning_patch": "",
        "trajectories": [],
    }
    out = engine.package_elite_telemetry(raw)
    parsed = json.loads(out)
    re_emitted = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    assert out == re_emitted


def test_load_task_raises_without_corpus(vault_path) -> None:
    """``load_task`` must hard-fail when no corpus is configured."""
    # Construct an engine with neither corpus_path nor the env var.
    import os

    import pytest

    from cathedral.v4.cathedral_engine import EngineError

    saved = os.environ.pop("CATHEDRAL_V4_CORPUS_PATH", None)
    try:
        e = CathedralEngine(vault_path=str(vault_path))
        with pytest.raises(EngineError):
            e.load_task("anything")
    finally:
        if saved is not None:
            os.environ["CATHEDRAL_V4_CORPUS_PATH"] = saved


def test_load_task_from_synthetic_fixture(vault_path, tmp_path) -> None:
    """``load_task`` reads the synthetic in-tree fixtures.

    Production corpus rows live OUTSIDE the public repo at
    ``CATHEDRAL_V4_CORPUS_PATH``. The synthetic fixtures under
    ``tests/v4/fixtures/synthetic_rows/`` are deliberately tiny and
    operator-checked-in so the test suite can run hermetically.
    """
    fixtures = (
        # tests/v4/fixtures/synthetic_rows/ -- committed in this PR
        # to exercise the load_task path without depending on
        # operator infra.
        __import__("pathlib").Path(__file__).parent / "fixtures" / "synthetic_rows"
    )
    e = CathedralEngine(vault_path=str(vault_path), corpus_path=str(fixtures))
    row = e.load_task("v4_synthetic_fastapi_001")
    assert row["task_id"] == "v4_synthetic_fastapi_001"
    assert row["base_repo"] == "python_fastapi_base"
    assert "bug_patch" in row
    assert "winning_patch_template" in row
    assert "hidden_test_template" in row


def test_bookkeeping_budget_for_telemetry_packaging(
    engine: CathedralEngine,
) -> None:
    """Pure-CPU packaging path stays well under the bookkeeping budget."""
    import time

    raw = {
        "task_id": "v4t_perf",
        "difficulty_tier": "bronze",
        "language": "python",
        "injected_fault_type": "x",
        "winning_patch": "--- a/x\n+++ b/x\n",
        "trajectories": [
            {
                "miner_hotkey": f"miner_{i}",
                "model_identifier": "echo-v1",
                "total_turns": 3,
                "outcome": "SUCCESS",
                "trace": [],
            }
            for i in range(10)
        ],
    }
    # warm
    for _ in range(3):
        engine.package_elite_telemetry(raw)
    t0 = time.monotonic()
    for _ in range(20):
        engine.package_elite_telemetry(raw)
    avg = (time.monotonic() - t0) / 20
    assert avg < BOOKKEEPING_BUDGET_SECONDS, (
        f"telemetry packaging avg={avg * 1000:.2f}ms exceeded "
        f"{BOOKKEEPING_BUDGET_SECONDS * 1000:.0f}ms bookkeeping budget"
    )
