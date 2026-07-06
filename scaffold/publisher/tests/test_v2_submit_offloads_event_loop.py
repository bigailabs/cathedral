"""Regression guard for the 2026-07-06 v2-beta timeout incident.

`/v2/agents/submit-bitset` is an async handler whose verify+admit body is sync
CPU (CNF regeneration, witness check) + sync DB. It used to run inline on the
event loop, so a submit stuck behind a slow per-miner CNF generation froze the
whole worker — /health and every other request hung until the client timed out
(miners saw ReadTimeout at 35s). The fix runs that body in a dedicated
ThreadPoolExecutor via run_in_executor, keeping the event loop free.

The full signed-submit contract (accept/202/verified, replay, bad-token,
sha/shape mismatch, witness-fail) is exercised by test_v2_kind_label_and_counter,
test_solution_manifest_v2, test_v2_solver_metadata and test_real_instance_bitset_e2e,
which all run THROUGH this offloaded path — so this file only guards the
structural property those suites can't assert: that the blocking body is
dispatched off the event loop.
"""
from __future__ import annotations

import inspect

from scaffold.publisher import app as app_mod


def test_submit_handler_offloads_to_dedicated_executor():
    # The handler must not run its blocking body on the event loop.
    src = inspect.getsource(app_mod.build_app)
    assert "run_in_executor(" in src, "submit body not offloaded via run_in_executor"
    assert "v2_submit_executor" in src, "dedicated submit executor missing"
    assert "ThreadPoolExecutor" in src, "no ThreadPoolExecutor"
