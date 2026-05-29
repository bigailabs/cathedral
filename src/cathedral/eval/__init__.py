"""Eval orchestration: pick queued submissions, spawn Polaris, score, sign."""

from cathedral.eval.eval_signer import EvalSigner
from cathedral.eval.orchestrator import EvalOrchestrator, run_eval_loop
from cathedral.eval.polaris_runner import (
    BundleCardRunner,
)
from cathedral.eval.runner_types import (
    PolarisRunner,
    PolarisRunnerError,
    PolarisRunResult,
    StubPolarisRunner,
)
from cathedral.eval.scoring_pipeline import score_and_sign
from cathedral.eval.task_generator import generate_task

__all__ = [
    "BundleCardRunner",
    "EvalOrchestrator",
    "EvalSigner",
    "PolarisRunResult",
    "PolarisRunner",
    "PolarisRunnerError",
    "StubPolarisRunner",
    "generate_task",
    "run_eval_loop",
    "score_and_sign",
]
