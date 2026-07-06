"""Unit tests for Stage 5 pair build."""
from __future__ import annotations

from scaffold.tests.test_distillation_corpus import _exports
from scaffold.distillation_corpus import assemble_corpus
from scaffold.distillation_pairs import build_pairs


def _corpus():
    return assemble_corpus(_exports(60))


def test_pairs_no_sensitive_markers():
    corpus = _corpus()
    pairs = build_pairs(corpus, split="train")
    blob = "\n".join(f"{p.input}\n{p.target}" for p in pairs.pairs).lower()
    for marker in ("https://", "github.com", "5h", "sc1", "secret-commit", "raw-witness"):
        assert marker not in blob


def test_pairs_deterministic():
    corpus = _corpus()
    a = build_pairs(corpus, split="train")
    b = build_pairs(corpus, split="train")
    assert a.pairs_hash == b.pairs_hash


def test_pairs_negative_is_first_class():
    corpus = _corpus()
    labels = set()
    for split in ("train", "val", "test"):
        for p in build_pairs(corpus, split=split).pairs:
            labels.add(p.label)
    assert "rejected_claim_negative_control" in labels


def test_pairs_no_test_leakage():
    corpus = _corpus()
    train = set(build_pairs(corpus, split="train").member_export_hashes)
    test = set(build_pairs(corpus, split="test").member_export_hashes)
    assert train.isdisjoint(test)


def test_pairs_carry_corpus_hash():
    corpus = _corpus()
    assert build_pairs(corpus, split="train").corpus_hash == corpus.corpus_hash
