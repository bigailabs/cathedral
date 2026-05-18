"""Perf budgets (revised v4 spec):

* ``BOOKKEEPING_BUDGET_SECONDS = 0.20`` -- pure-CPU envelope work
  (load, scramble, package telemetry, hash, sign-bytes). Asserted
  by ``test_bookkeeping_p99_under_budget``.
* ``REPRO_BUDGET_SECONDS = 3.0`` -- the publisher-side oracle
  subprocess that runs the hidden test against a miner patch.
  Asserted by ``test_repro_p99_under_budget``. Realistic ceiling
  for FastAPI / Django / Prisma test runs (per the revised spec,
  200ms is too tight for general pytest; the bookkeeping budget
  stays at 200ms but the actual repro gets 3s).
"""

from __future__ import annotations

import re
import time
from statistics import quantiles

from cathedral.v4.cathedral_engine import CathedralEngine
from cathedral.v4.oracle.patch_runner import (
    BOOKKEEPING_BUDGET_SECONDS,
    REPRO_BUDGET_SECONDS,
)


def _render(template: str, rename_map: dict[str, str]) -> str:
    def sub(match: re.Match[str]) -> str:
        return rename_map.get(match.group(1), match.group(1))

    return re.sub(r"\{\{rename:([A-Za-z_][A-Za-z0-9_]*)\}\}", sub, template)


def test_bookkeeping_p99_under_budget(engine: CathedralEngine, python_manifest: dict) -> None:
    """Engine bookkeeping (scramble + bundle build + telemetry) stays
    under the 200ms bookkeeping budget at p99 over 50 iterations.
    """
    raw = {
        "task_id": "v4t_bookkeep",
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
            for i in range(20)
        ],
    }

    # warmup
    for _ in range(3):
        engine.load_and_scramble_task("python_fastapi_base")
        engine.package_elite_telemetry(raw)

    durations: list[float] = []
    for _ in range(50):
        t0 = time.monotonic()
        engine.load_and_scramble_task("python_fastapi_base")
        engine.package_elite_telemetry(raw)
        durations.append(time.monotonic() - t0)

    durations.sort()
    p50 = durations[len(durations) // 2]
    p99 = quantiles(durations, n=100, method="inclusive")[-1]
    p_max = durations[-1]

    print(
        f"\n[v4 bench bookkeeping] iters=50 p50={p50 * 1000:.1f}ms "
        f"p99={p99 * 1000:.1f}ms max={p_max * 1000:.1f}ms "
        f"budget={BOOKKEEPING_BUDGET_SECONDS * 1000:.0f}ms",
    )
    assert p99 < BOOKKEEPING_BUDGET_SECONDS, (
        f"v4 bookkeeping p99={p99 * 1000:.1f}ms exceeded {BOOKKEEPING_BUDGET_SECONDS * 1000:.0f}ms"
    )


def test_repro_p99_under_budget(engine: CathedralEngine, python_manifest: dict) -> None:
    """Publisher-side oracle subprocess (verify_miner_submission) stays
    under the 3s repro budget at p99 over 20 iterations.

    20 iterations rather than 50 because each iteration spawns a
    real subprocess; 50 would push wall clock past test-run
    comfort.
    """
    task = engine.load_and_scramble_task("python_fastapi_base")
    rename_map = task["rename_map"]
    winning_patch = _render(python_manifest["winning_patch_template"], rename_map)
    hidden_test = _render(python_manifest["hidden_test_template"], rename_map)
    original = task["original_repo_state"]

    # warmup
    for _ in range(3):
        engine.verify_miner_submission(original, winning_patch, hidden_test)

    durations: list[float] = []
    for _ in range(20):
        t0 = time.monotonic()
        passed, _ = engine.verify_miner_submission(original, winning_patch, hidden_test)
        assert passed, "winning patch should pass in bench"
        durations.append(time.monotonic() - t0)

    durations.sort()
    p50 = durations[len(durations) // 2]
    p99 = quantiles(durations, n=100, method="inclusive")[-1]
    p_max = durations[-1]

    print(
        f"\n[v4 bench repro] iters=20 p50={p50 * 1000:.1f}ms "
        f"p99={p99 * 1000:.1f}ms max={p_max * 1000:.1f}ms "
        f"budget={REPRO_BUDGET_SECONDS * 1000:.0f}ms",
    )
    assert p99 < REPRO_BUDGET_SECONDS, (
        f"v4 repro p99={p99 * 1000:.1f}ms exceeded {REPRO_BUDGET_SECONDS * 1000:.0f}ms"
    )
