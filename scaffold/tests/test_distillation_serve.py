"""Unit tests for Stage 7 serve + eval + revenue gate."""
from __future__ import annotations

import pytest

from scaffold.tests.test_distillation_corpus import _exports
from scaffold.distillation_corpus import assemble_corpus
from scaffold.distillation_pairs import build_pairs
from scaffold.distillation_train import train
from scaffold.distillation_serve import (
    EvalConfig,
    LeakageError,
    ServeConfig,
    evaluate,
    serving_manifest,
)


def _setup():
    corpus = assemble_corpus(_exports(60))
    train_pairs = build_pairs(corpus, split="train")
    test_pairs = build_pairs(corpus, split="test")
    artifact = train(train_pairs, dry_run=True)
    return corpus, train_pairs, test_pairs, artifact


_PASS = EvalConfig(min_accuracy=0.5, min_positive_recall=0.0, must_beat_baseline=False)


def test_eval_test_split_only():
    _, train_pairs, _, artifact = _setup()
    with pytest.raises(LeakageError):
        evaluate(artifact, train_pairs)


def test_eval_rejects_degenerate_classifier():
    _, _, test_pairs, artifact = _setup()
    report = evaluate(artifact, test_pairs, predict=None)  # majority-class stub
    assert not report.beats_baseline


def test_serve_requires_eval():
    _, _, _, artifact = _setup()
    sm = serving_manifest(artifact, None)
    assert not sm["ready"]
    assert sm["state"] == "unevaluated"


def test_serve_below_threshold_not_ready():
    _, _, test_pairs, artifact = _setup()
    report = evaluate(artifact, test_pairs, predict=None)
    sm = serving_manifest(artifact, report)  # default EvalConfig requires beating baseline
    assert not sm["ready"]


def _perfect(test_pairs):
    def f(inp: str) -> str:
        for p in test_pairs.pairs:
            if p.input == inp:
                return p.label
        return "rejected_claim_negative_control"
    return f


def test_earning_requires_full_evidence():
    _, _, test_pairs, artifact = _setup()
    report = evaluate(artifact, test_pairs, predict=_perfect(test_pairs), config=_PASS)
    no_receipt = serving_manifest(
        artifact, report, config=ServeConfig(eval=_PASS),
        deployment_id="d", auth="a", health_receipt={"timestamp": 100}, now=200,
    )
    assert not no_receipt["earning"]

    earning = serving_manifest(
        artifact, report, config=ServeConfig(eval=_PASS),
        deployment_id="d", auth="a",
        health_receipt={"timestamp": 100},
        usage_receipt={"timestamp": 100, "receipt_hash": "h", "receipt_source": "chutes"},
        now=200,
    )
    assert earning["earning"]
    assert earning["receipt_hash"] == "h"


def test_stale_receipt_demotes():
    _, _, test_pairs, artifact = _setup()
    report = evaluate(artifact, test_pairs, predict=_perfect(test_pairs), config=_PASS)
    sm = serving_manifest(
        artifact, report,
        config=ServeConfig(eval=_PASS, receipt_max_age_seconds=10),
        deployment_id="d", auth="a",
        health_receipt={"timestamp": 100},
        usage_receipt={"timestamp": 100, "receipt_hash": "h", "receipt_source": "chutes"},
        now=100_000,
    )
    assert sm["state"] == "stale"
    assert not sm["earning"]


def test_gated_by_default():
    _, _, test_pairs, artifact = _setup()
    report = evaluate(artifact, test_pairs, predict=_perfect(test_pairs), config=_PASS)
    sm = serving_manifest(artifact, report, config=ServeConfig(eval=_PASS))
    assert sm["gated"] is True


def test_forged_eval_report_rejected():
    import dataclasses
    _, _, test_pairs, artifact = _setup()
    report = evaluate(artifact, test_pairs, predict=_perfect(test_pairs), config=_PASS)
    forged = dataclasses.replace(report, eval_hash="FORGED")
    sm = serving_manifest(
        artifact, forged, config=ServeConfig(eval=_PASS),
        deployment_id="d", auth="a",
        health_receipt={"timestamp": 100},
        usage_receipt={"timestamp": 100, "receipt_hash": "h", "receipt_source": "c"},
        now=200,
    )
    assert not sm["earning"]
    assert sm["state"] == "unevaluated"


def test_eval_report_bound_to_artifact():
    import dataclasses
    _, _, test_pairs, artifact = _setup()
    report = evaluate(artifact, test_pairs, predict=_perfect(test_pairs), config=_PASS)
    # A different artifact identity must invalidate the report binding.
    other = dataclasses.replace(artifact, artifact_sha256="different")
    sm = serving_manifest(
        other, report, config=ServeConfig(eval=_PASS),
        deployment_id="d", auth="a",
        health_receipt={"timestamp": 100},
        usage_receipt={"timestamp": 100, "receipt_hash": "h", "receipt_source": "c"},
        now=200,
    )
    assert not sm["earning"]


def test_future_receipt_not_fresh():
    _, _, test_pairs, artifact = _setup()
    report = evaluate(artifact, test_pairs, predict=_perfect(test_pairs), config=_PASS)
    sm = serving_manifest(
        artifact, report, config=ServeConfig(eval=_PASS),
        deployment_id="d", auth="a",
        health_receipt={"timestamp": 9999},  # future
        now=100,
    )
    assert not sm["earning"]
