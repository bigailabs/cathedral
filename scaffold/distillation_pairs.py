"""Stage 5: training-pair build.

Converts a Corpus (Stage 4) into model-ready supervised pairs for a chosen
split. Format-only; no training. Returns a PairsManifest that carries lineage
(corpus_hash) forward so Stage 6 can verify provenance without re-reading the
corpus.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from scaffold.distillation_corpus import Corpus, verify_corpus_integrity


PAIRS_SCHEMA_VERSION = "cathedral.distillation_pairs.v1"
_VALID_SPLITS = ("train", "val", "test")

# Substrings that would indicate raw sensitive material leaked into a pair.
# We do NOT guess SS58 hotkey prefixes (5F/5G...) because redacted hex hashes
# legitimately contain those chars. Instead we forbid raw URLs, git markers,
# and the literal raw-witness sentinel used upstream.
_FORBIDDEN_MARKERS = ("http://", "https://", "github.com", "secret-commit", "raw-witness")


@dataclass(frozen=True)
class PairFormat:
    name: str = "witness_judgement_v1"

    def canonical(self) -> dict[str, Any]:
        return {"name": self.name}


@dataclass(frozen=True)
class TrainingPair:
    input: str
    target: str
    label: str
    weight: float
    provenance_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "input": self.input,
            "target": self.target,
            "label": self.label,
            "weight": self.weight,
            "provenance_hash": self.provenance_hash,
        }


@dataclass(frozen=True)
class PairsManifest:
    pairs: tuple[TrainingPair, ...]
    split: str
    corpus_hash: str
    pairs_hash: str
    format_hash: str
    member_export_hashes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PAIRS_SCHEMA_VERSION,
            "split": self.split,
            "corpus_hash": self.corpus_hash,
            "pairs_hash": self.pairs_hash,
            "format_hash": self.format_hash,
            "member_export_hashes": list(self.member_export_hashes),
            "pairs": [p.to_dict() for p in self.pairs],
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash_obj(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _member_context(member: dict[str, Any]) -> str:
    task = member.get("task", {})
    return (
        f"invariant_id={task.get('invariant_id','')} "
        f"invariant_hash={task.get('invariant_hash','')} "
        f"decode_kind={task.get('decode_kind','')} "
        f"severity_hint={task.get('severity_hint','')} "
        f"replay_kind={task.get('replay_kind','')}"
    )


def _build_pair(member: dict[str, Any], fmt: PairFormat) -> TrainingPair:
    accepted = bool(member.get("supervision", {}).get("accepted"))
    context = _member_context(member)
    if accepted:
        target = "reproduces: this witness is a valid, replayable exploit path."
        label = "accepted_reproduced_witness"
    else:
        reason = str(member.get("supervision", {}).get("rejection_reason") or "unspecified")
        target = f"does_not_reproduce: rejected ({reason})."
        label = "rejected_claim_negative_control"
    return TrainingPair(
        input=f"Assess this audit witness. {context}",
        target=target,
        label=label,
        weight=1.0,
        provenance_hash=member["export_hash"],
    )


def _assert_no_sensitive_markers(pair: TrainingPair) -> None:
    blob = f"{pair.input}\n{pair.target}".lower()
    for marker in _FORBIDDEN_MARKERS:
        if marker.lower() in blob:
            raise ValueError(f"pair_contains_sensitive_marker:{marker}")


def build_pairs(corpus: Corpus, *, fmt: PairFormat | None = None, split: str) -> PairsManifest:
    if split not in _VALID_SPLITS:
        raise ValueError(f"invalid_split:{split}")
    # Reject a corpus mutated after assembly (finding #2): recompute and match.
    verify_corpus_integrity(corpus)
    fmt = fmt or PairFormat()
    members = corpus.members_for_split(split)
    pairs = tuple(_build_pair(m, fmt) for m in members)
    for p in pairs:
        _assert_no_sensitive_markers(p)
    pairs_hash = recompute_pairs_hash(pairs)
    return PairsManifest(
        pairs=pairs,
        split=split,
        corpus_hash=corpus.corpus_hash,
        pairs_hash=pairs_hash,
        format_hash=_hash_obj(fmt.canonical()),
        member_export_hashes=tuple(m["export_hash"] for m in members),
    )


def recompute_pairs_hash(pairs: tuple[TrainingPair, ...]) -> str:
    return _hash_obj([p.to_dict() for p in pairs])


def verify_pairs_manifest(manifest: PairsManifest) -> None:
    """Raise if a PairsManifest is internally inconsistent (finding #3)."""
    if not getattr(manifest, "corpus_hash", ""):
        raise ValueError("pairs_manifest_missing_corpus_hash")
    if recompute_pairs_hash(manifest.pairs) != manifest.pairs_hash:
        raise ValueError("pairs_hash_mismatch")
    declared = set(manifest.member_export_hashes)
    provenance = {p.provenance_hash for p in manifest.pairs}
    if not provenance.issubset(declared):
        raise ValueError("pair_provenance_not_in_member_set")
