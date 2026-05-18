"""End-to-end with the three synthetic in-tree fixtures.

Exercises ``load_task -> build_miner_bundle -> verify_miner_submission``
against each of the synthetic fixture rows. These rows live in
``tests/v4/fixtures/synthetic_rows/`` and are deliberately tiny;
the production v4 corpus lives outside the public repo at
``$CATHEDRAL_V4_CORPUS_PATH``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cathedral.v4.cathedral_engine import CathedralEngine

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic_rows"
SYNTHETIC_IDS = [
    "v4_synthetic_fastapi_001",
    "v4_synthetic_fastapi_002",
    "v4_synthetic_fastapi_003",
]


def _render(template: str, rename_map: dict[str, str]) -> str:
    def sub(match: re.Match[str]) -> str:
        return rename_map.get(match.group(1), match.group(1))

    return re.sub(r"\{\{rename:([A-Za-z_][A-Za-z0-9_]*)\}\}", sub, template)


@pytest.fixture
def corpus_engine(vault_path: Path) -> CathedralEngine:
    return CathedralEngine(vault_path=str(vault_path), corpus_path=str(FIXTURES))


@pytest.mark.parametrize("task_id", SYNTHETIC_IDS)
def test_synthetic_row_end_to_end(corpus_engine: CathedralEngine, task_id: str) -> None:
    task = corpus_engine.load_task(task_id)
    assert task["task_id"] == task_id

    bundle, handle = corpus_engine.build_bundle_and_handle(
        base_repo=task["base_repo"],
        bug_patch=task["bug_patch"],
        seed=task["seed"],
    )
    # Broken state must differ from clean state.
    assert bundle.workspace_files != handle.clean_state, (
        f"build_bundle_and_handle did not apply bug for {task_id}"
    )

    # Render hidden test against scrambled rename_map (publisher-side).
    hidden_test = _render(task["hidden_test_template"], handle.rename_map)
    winning_patch = _render(task["winning_patch_template"], handle.rename_map)

    # Verify the broken bundle FAILS the hidden test.
    noop_diff = "--- a/_pin\n+++ a/_pin\n"  # malformed -> patch_applied=False
    failed_broken, _ = corpus_engine.verify_miner_submission(
        original_repo_state=bundle.workspace_files,
        patch_str=noop_diff,
        hidden_test_code=hidden_test,
    )
    assert failed_broken is False, (
        f"hidden test passed against BROKEN state for {task_id}, bug_patch may not be biting"
    )

    # Verify the winning patch FIXES the broken bundle.
    fixed, duration = corpus_engine.verify_miner_submission(
        original_repo_state=bundle.workspace_files,
        patch_str=winning_patch,
        hidden_test_code=hidden_test,
    )
    assert fixed is True, (
        f"winning patch failed against broken state for {task_id} "
        f"(duration={duration * 1000:.0f}ms)"
    )
