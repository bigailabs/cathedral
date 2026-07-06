"""Stage 6: fine-tune (dry-run + real).

Produces a ModelArtifact manifest from a PairsManifest. Dry-run mode validates
config and binds lineage without touching a GPU or network, so CI can gate it.
A real run performs a LoRA/adapter fine-tune; that path is config-driven and not
exercised by the offline gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any

from scaffold.distillation_corpus import Corpus
from scaffold.distillation_pairs import PairsManifest, verify_pairs_manifest


MODEL_SCHEMA_VERSION = "cathedral.distillation_model.v1"


class UnprovenancedTrainingError(ValueError):
    """Raised when a PairsManifest lacks the lineage needed to train."""


@dataclass(frozen=True)
class TrainConfig:
    base_model: str = "cathedral/small-open-base"
    base_model_license: str = "apache-2.0"
    adapter_kind: str = "lora"
    epochs: int = 1
    learning_rate: float = 1e-4
    extra: dict[str, Any] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        return {
            "base_model": self.base_model,
            "base_model_license": self.base_model_license,
            "adapter_kind": self.adapter_kind,
            "epochs": int(self.epochs),
            "learning_rate": float(self.learning_rate),
            "extra": self.extra,
        }


@dataclass(frozen=True)
class ModelArtifact:
    base_model: str
    base_model_license: str
    adapter_kind: str
    corpus_hash: str
    pairs_hash: str
    train_config_hash: str
    artifact_sha256: str
    created_by: str
    eval: Any = None  # populated in EvalReport / serving manifest, not here

    def to_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": MODEL_SCHEMA_VERSION,
            "base_model": self.base_model,
            "base_model_license": self.base_model_license,
            "adapter_kind": self.adapter_kind,
            "corpus_hash": self.corpus_hash,
            "pairs_hash": self.pairs_hash,
            "train_config_hash": self.train_config_hash,
            "artifact_sha256": self.artifact_sha256,
            "eval": self.eval,
            "created_by": self.created_by,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash_obj(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_lineage(pairs_manifest: PairsManifest, corpus: Corpus | None) -> None:
    if not getattr(pairs_manifest, "corpus_hash", ""):
        raise UnprovenancedTrainingError("pairs_manifest_missing_corpus_hash")
    if not getattr(pairs_manifest, "pairs_hash", ""):
        raise UnprovenancedTrainingError("pairs_manifest_missing_pairs_hash")
    if getattr(pairs_manifest, "split", None) != "train":
        raise UnprovenancedTrainingError(
            f"train_requires_train_split:{getattr(pairs_manifest, 'split', None)}"
        )
    # Verify against the trusted corpus when provided: this proves the manifest
    # really is the train split of that corpus, defeating a relabel-and-rehash
    # split swap. Without a corpus we can only prove internal consistency.
    try:
        verify_pairs_manifest(pairs_manifest, corpus)
    except (ValueError, AttributeError, TypeError) as exc:
        raise UnprovenancedTrainingError(f"pairs_manifest_inconsistent:{exc}") from exc


def train(
    pairs_manifest: PairsManifest,
    *,
    corpus: Corpus | None = None,
    config: TrainConfig | None = None,
    dry_run: bool = False,
    created_by: str = "operator",
) -> ModelArtifact:
    config = config or TrainConfig()
    _require_lineage(pairs_manifest, corpus)
    train_config_hash = _hash_obj(config.canonical())

    if dry_run:
        artifact_sha256 = "dry-run"
    else:  # pragma: no cover - real training path, not exercised offline
        artifact_sha256 = _real_finetune(pairs_manifest, config)

    return ModelArtifact(
        base_model=config.base_model,
        base_model_license=config.base_model_license,
        adapter_kind=config.adapter_kind,
        corpus_hash=pairs_manifest.corpus_hash,
        pairs_hash=pairs_manifest.pairs_hash,
        train_config_hash=train_config_hash,
        artifact_sha256=artifact_sha256,
        created_by=created_by,
        eval=None,
    )


def _real_finetune(pairs_manifest: PairsManifest, config: TrainConfig) -> str:  # pragma: no cover
    raise NotImplementedError(
        "real fine-tune runs on Kaggle TPU / rented GPU; the offline gate uses dry_run=True"
    )
