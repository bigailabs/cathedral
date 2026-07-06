"""Unit tests for Stage 6 train (dry-run)."""
from __future__ import annotations

import pytest

from scaffold.tests.test_distillation_corpus import _exports
from scaffold.distillation_corpus import assemble_corpus
from scaffold.distillation_pairs import build_pairs
from scaffold.distillation_train import TrainConfig, UnprovenancedTrainingError, train


def _train_pairs():
    return build_pairs(assemble_corpus(_exports(60)), split="train")


def test_dry_run_produces_manifest_no_gpu():
    art = train(_train_pairs(), dry_run=True)
    assert art.artifact_sha256 == "dry-run"
    m = art.to_manifest()
    assert m["schema_version"] == "cathedral.distillation_model.v1"


def test_manifest_binds_lineage():
    tp = _train_pairs()
    art = train(tp, dry_run=True)
    m = art.to_manifest()
    assert m["corpus_hash"] == tp.corpus_hash
    assert m["pairs_hash"] == tp.pairs_hash
    assert m["train_config_hash"]


def test_rejects_unprovenanced():
    class Fake:
        corpus_hash = ""
        pairs_hash = ""
        split = "train"

    with pytest.raises(UnprovenancedTrainingError):
        train(Fake(), dry_run=True)


def test_rejects_non_train_split():
    corpus = assemble_corpus(_exports(60))
    test_pairs = build_pairs(corpus, split="test")
    with pytest.raises(UnprovenancedTrainingError):
        train(test_pairs, dry_run=True)


def test_records_base_model_license():
    art = train(_train_pairs(), config=TrainConfig(base_model_license="mit"), dry_run=True)
    assert art.to_manifest()["base_model_license"] == "mit"


def test_rejects_forged_pairs_hash():
    import dataclasses
    tp = _train_pairs()
    forged = dataclasses.replace(tp, pairs_hash="FORGED")
    with pytest.raises(UnprovenancedTrainingError):
        train(forged, dry_run=True)


def test_rejects_forged_pairs_content():
    # Same hashes, but pairs swapped so provenance no longer matches members.
    import dataclasses
    from scaffold.distillation_pairs import build_pairs as _bp
    corpus = assemble_corpus(_exports(60))
    tp = _bp(corpus, split="train")
    other = _bp(corpus, split="test").pairs
    tampered = dataclasses.replace(tp, pairs=other)  # pairs_hash now stale
    with pytest.raises(UnprovenancedTrainingError):
        train(tampered, dry_run=True)
