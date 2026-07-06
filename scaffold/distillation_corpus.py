"""Stage 4: distillation corpus assembly.

Consumes ``cathedral.audit_trace.export.v1`` records (from
``scaffold.distillation.export_trace``) and produces a deterministic,
deduplicated, split training corpus.

CRITICAL (Codex finding #1): ``export.v1`` is NOT automatically training-safe.
The exporter keeps ``task.repo_url``/``task.commit`` in PRIVATE exports and a
private-audience policy can include the raw agent trace / decoded witness /
replay artifacts. This module applies a ``training_safe_view`` gate that rejects
unsafe exports and strips/hashes repo/commit before anything becomes a member.
Redaction is enforced HERE, not assumed from upstream.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Iterable


EXPORT_SCHEMA_VERSION = "cathedral.audit_trace.export.v1"
SOURCE_TRACE_SCHEMA_VERSION = "cathedral.audit_trace.v1"
CORPUS_SCHEMA_VERSION = "cathedral.distillation_corpus.v1"

_SPLIT_MODULUS = 1000
_VALID_SPLITS = ("train", "val", "test")


class UnsafeExportError(ValueError):
    """Raised when an export is not safe to use as training data."""


class NotAnExportError(ValueError):
    """Raised when the input is a raw trace or otherwise not an export.v1."""


@dataclass(frozen=True)
class CorpusConfig:
    dedup_by: str = "export_hash"
    split: dict[str, float] = field(
        default_factory=lambda: {"train": 0.8, "val": 0.1, "test": 0.1}
    )
    split_salt: str = "cathedral-distillation-v1"
    balance_max_negative_ratio: float | None = None
    min_members: int = 1
    audience: str = "private"  # private | public

    def canonical(self) -> dict[str, Any]:
        return {
            "dedup_by": self.dedup_by,
            "split": {k: float(self.split[k]) for k in sorted(self.split)},
            "split_salt": self.split_salt,
            "balance_max_negative_ratio": self.balance_max_negative_ratio,
            "min_members": int(self.min_members),
            "audience": self.audience,
        }


@dataclass(frozen=True)
class Corpus:
    members: tuple[dict[str, Any], ...]
    split_assignments: dict[str, str]  # export_hash -> split
    config: CorpusConfig
    member_set_hash: str
    corpus_hash: str
    drops: dict[str, int]

    def stats(self) -> dict[str, Any]:
        per_category: dict[str, int] = {}
        per_split: dict[str, int] = {s: 0 for s in _VALID_SPLITS}
        for member in self.members:
            cat = str(member.get("dataset", {}).get("category") or "unknown")
            per_category[cat] = per_category.get(cat, 0) + 1
            per_split[self.split_assignments[member["export_hash"]]] += 1
        return {
            "n_members": len(self.members),
            "per_category": per_category,
            "per_split": per_split,
            "drops": dict(self.drops),
        }

    def members_for_split(self, split: str) -> tuple[dict[str, Any], ...]:
        if split not in _VALID_SPLITS:
            raise ValueError(f"invalid_split:{split}")
        return tuple(
            m for m in self.members
            if self.split_assignments[m["export_hash"]] == split
        )

    def to_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": CORPUS_SCHEMA_VERSION,
            "member_set_hash": self.member_set_hash,
            "corpus_hash": self.corpus_hash,
            "config": self.config.canonical(),
            "split_assignments": dict(sorted(self.split_assignments.items())),
            "member_export_hashes": sorted(m["export_hash"] for m in self.members),
            "stats": self.stats(),
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_obj(value: Any) -> str:
    return _sha256(_canonical_json(value))


def _is_export(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and record.get("schema_version") == EXPORT_SCHEMA_VERSION
        and isinstance(record.get("redaction"), dict)
    )


def training_safe_view(export: dict[str, Any]) -> dict[str, Any]:
    """Return a training-safe member from an export.v1 record.

    Raises NotAnExportError for raw traces / non-exports.
    Raises UnsafeExportError if the export carries raw witness / agent trace /
    replay artifacts. Strips repo_url/commit so no member keeps raw target ids.
    """
    if not _is_export(export):
        raise NotAnExportError("input_is_not_export_v1")

    redaction = export.get("redaction", {})
    if (
        bool(redaction.get("raw_witness_included"))
        or bool(redaction.get("agent_trace_included"))
        or bool(redaction.get("replay_artifacts_included"))
    ):
        raise UnsafeExportError("export_includes_raw_sensitive_material")

    export_hash = str(export.get("export_hash") or "")
    if not export_hash:
        raise NotAnExportError("export_missing_export_hash")

    # Recompute export_hash over the body without its own field (matches
    # scaffold/distillation.py where export_hash is computed before attachment).
    body = {k: v for k, v in export.items() if k != "export_hash"}
    if _hash_obj(body) != export_hash:
        raise UnsafeExportError("export_hash_mismatch")

    source_schema = str(export.get("source_schema_version") or "")
    source_trace_hash = str(export.get("source_trace_hash") or "")
    if source_schema != SOURCE_TRACE_SCHEMA_VERSION or not source_trace_hash:
        raise UnsafeExportError("export_missing_source_trace_provenance")

    # Whitelist ONLY known-safe, already-redacted fields (finding #1). We do not
    # copy submission/verdict/supervision/dataset wholesale, because a free-text
    # field with the flags set false could still carry raw material. Each field
    # below is either a hash, an enum, or a bounded controlled value.
    raw_task = export.get("task") or {}
    task = {
        # repo_url / commit deliberately excluded (never copied).
        "task_id_hash": _safe_str(raw_task.get("task_id_hash")),
        "target_id_hash": _safe_str(raw_task.get("target_id_hash")),
        "invariant_id": _safe_str(raw_task.get("invariant_id")),
        "invariant_hash": _safe_str(raw_task.get("invariant_hash")),
        "challenge_id_hash": _safe_str(raw_task.get("challenge_id_hash")),
        "cnf_sha256": _safe_str(raw_task.get("cnf_sha256")),
        "decode_kind": _safe_str(raw_task.get("decode_kind")),
        "severity_hint": _safe_enum(raw_task.get("severity_hint"),
                                    {"low", "medium", "high", "critical", ""}),
        "replay_kind": _safe_str(raw_task.get("replay_kind")),
    }

    raw_sub = export.get("submission") or {}
    submission = {
        "miner_hotkey_hash": _safe_str(raw_sub.get("miner_hotkey_hash")),
        "dimacs_solution_sha256": _safe_str(raw_sub.get("dimacs_solution_sha256")),
        # agent_trace_hash only; never the raw agent_trace.
        "agent_trace_hash": _safe_str(raw_sub.get("agent_trace_hash")),
    }

    raw_verdict = export.get("verdict") or {}
    verdict = {
        "accepted": bool(raw_verdict.get("accepted")),
        "stage": _safe_str(raw_verdict.get("stage")),
        "rejection_reason": _rejection_category(raw_verdict.get("rejection_reason")),
        "decoded_witness_hash": _safe_str(
            (raw_verdict.get("decoded_witness_hash") or "")
        ),
    }

    raw_sup = export.get("supervision") or {}
    supervision = {
        "accepted": bool(raw_sup.get("accepted")),
        "stage": _safe_str(raw_sup.get("stage")),
        "rejection_reason": _rejection_category(raw_sup.get("rejection_reason")),
        "training_value": _safe_enum(
            raw_sup.get("training_value"),
            {"positive_replay_example", "valuable_negative_control", ""},
        ),
    }

    raw_ds = export.get("dataset") or {}
    dataset = {
        "category": _safe_enum(
            raw_ds.get("category"),
            {"accepted_reproduced_witness", "rejected_claim_negative_control",
             "unknown_audit_trace", ""},
        ),
        "retention_value": _safe_str(raw_ds.get("retention_value")),
    }

    member = {
        "export_hash": export_hash,
        "source_trace_hash": source_trace_hash,
        "source_schema_version": source_schema,
        "audience": _safe_enum(export.get("audience"), {"private", "public"}),
        "disclosure_status": _safe_str(export.get("disclosure_status")) or "private",
        "task": task,
        "submission": submission,
        "verdict": verdict,
        "supervision": supervision,
        "dataset": dataset,
    }
    # Defense in depth: assert no sensitive marker survived the whitelist.
    _assert_member_clean(member)
    # Bind full member content so post-assembly mutation is detected (finding: a
    # mutated supervision.accepted must not pass integrity). member_hash is
    # computed over everything EXCEPT itself.
    member["member_hash"] = _hash_obj(member)
    return member


# Controlled vocabulary for rejection reasons so raw free-text can never ride
# along in a pair target (finding #1).
_REJECTION_CATEGORIES = {
    "cnf_unsatisfied", "decode_failed", "replay_not_reproduced",
    "stale_package", "invalid_submission", "unspecified",
}


def _rejection_category(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    for cat in _REJECTION_CATEGORIES:
        if cat in text:
            return cat
    return "unspecified"


def _safe_str(value: Any) -> str:
    return str(value or "")


def _safe_enum(value: Any, allowed: set[str]) -> str:
    text = str(value or "")
    return text if text in allowed else ""


# Markers that must never appear anywhere in a training-safe member.
_SENSITIVE_SUBSTRINGS = ("http://", "https://", "github.com", "secret-commit", "raw-witness")


def _assert_member_clean(member: dict[str, Any]) -> None:
    blob = _canonical_json(member).lower()
    for marker in _SENSITIVE_SUBSTRINGS:
        if marker in blob:
            raise UnsafeExportError(f"member_contains_sensitive_marker:{marker}")


def _assign_split(export_hash: str, config: CorpusConfig) -> str:
    bucket = int(_sha256(export_hash + config.split_salt), 16) % _SPLIT_MODULUS
    train_r = config.split.get("train", 0.0)
    val_r = config.split.get("val", 0.0)
    train_cut = int(round(train_r * _SPLIT_MODULUS))
    val_cut = train_cut + int(round(val_r * _SPLIT_MODULUS))
    if bucket < train_cut:
        return "train"
    if bucket < val_cut:
        return "val"
    return "test"


_PUBLIC_DISCLOSURE_ALLOWED = {"fixed", "public", "opt_in", "cathedral_owned"}


def assemble_corpus(
    exports: Iterable[dict[str, Any]],
    *,
    config: CorpusConfig | None = None,
) -> Corpus:
    config = config or CorpusConfig()
    seen: dict[str, dict[str, Any]] = {}
    drops = {"duplicates": 0, "unsafe": 0, "not_export": 0}

    for record in exports:
        try:
            member = training_safe_view(record)
        except NotAnExportError:
            drops["not_export"] += 1
            raise
        except UnsafeExportError:
            drops["unsafe"] += 1
            raise
        key = member["export_hash"] if config.dedup_by == "export_hash" else _hash_obj(member)
        if key in seen:
            drops["duplicates"] += 1
            continue
        seen[key] = member

    members = tuple(sorted(seen.values(), key=lambda m: m["export_hash"]))

    if config.audience == "public":
        for m in members:
            # Public corpus requires BOTH: the member was exported for public
            # audience AND its disclosure status is in the allowed set (finding #8).
            if m["audience"] != "public":
                raise UnsafeExportError("public_corpus_requires_public_audience_members")
            if m["disclosure_status"] not in _PUBLIC_DISCLOSURE_ALLOWED:
                raise UnsafeExportError("public_corpus_requires_all_members_disclosure_gated")

    if len(members) < config.min_members:
        raise ValueError(f"corpus_below_min_members:{len(members)}<{config.min_members}")

    if config.balance_max_negative_ratio is not None:
        pos = sum(1 for m in members if m["supervision"].get("accepted"))
        neg = len(members) - pos
        if pos > 0 and neg / max(pos, 1) > config.balance_max_negative_ratio:
            raise ValueError("corpus_negative_ratio_exceeds_cap")

    split_assignments = {
        m["export_hash"]: _assign_split(m["export_hash"], config) for m in members
    }
    member_set_hash = recompute_member_set_hash(members)
    corpus_hash = _hash_obj(
        {
            "schema_version": CORPUS_SCHEMA_VERSION,
            "config": config.canonical(),
            "split_assignments": dict(sorted(split_assignments.items())),
            "member_set_hash": member_set_hash,
        }
    )
    return Corpus(
        members=members,
        split_assignments=split_assignments,
        config=config,
        member_set_hash=member_set_hash,
        corpus_hash=corpus_hash,
        drops=drops,
    )


def _member_content_hash(member: dict[str, Any]) -> str:
    """Recompute a member's content hash (over everything except member_hash)."""
    body = {k: v for k, v in member.items() if k != "member_hash"}
    return _hash_obj(body)


def recompute_member_set_hash(members: tuple[dict[str, Any], ...]) -> str:
    # Bind full member content (member_hash), not just export_hash, so mutating
    # any member field changes the set hash.
    return _hash_obj(sorted(_member_content_hash(m) for m in members))


def recompute_corpus_hash(corpus: Corpus) -> str:
    """Recompute corpus_hash from the corpus's own members/config/splits.

    Used to detect tampering after assembly (finding #2): if a caller mutated a
    member dict or a split assignment, the recomputed hash won't match the
    stored one.
    """
    member_set_hash = recompute_member_set_hash(corpus.members)
    return _hash_obj(
        {
            "schema_version": CORPUS_SCHEMA_VERSION,
            "config": corpus.config.canonical(),
            "split_assignments": dict(sorted(corpus.split_assignments.items())),
            "member_set_hash": member_set_hash,
        }
    )


def verify_corpus_integrity(corpus: Corpus) -> None:
    """Raise if the corpus was mutated after assembly.

    Binds FULL member content: recomputing each member_hash catches mutation of
    any member field (e.g. supervision.accepted, which changes pair labels).
    """
    for m in corpus.members:
        stored = m.get("member_hash")
        if not stored or _member_content_hash(m) != stored:
            raise UnsafeExportError("corpus_member_content_mutated")
        _assert_member_clean(m)
    if recompute_member_set_hash(corpus.members) != corpus.member_set_hash:
        raise UnsafeExportError("corpus_member_set_hash_mismatch")
    if recompute_corpus_hash(corpus) != corpus.corpus_hash:
        raise UnsafeExportError("corpus_hash_mismatch")
