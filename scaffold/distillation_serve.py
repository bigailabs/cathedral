"""Stage 7: serve + eval + revenue gate.

Evaluates a model on the held-out test split and gates it for serving through a
Lane-2-style evidence state machine. A model earns only when eval passes AND a
healthy deployment AND a usage/revenue receipt all exist. Revenue is off-chain;
no emissions writes, ever.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Callable

from scaffold.distillation_corpus import Corpus
from scaffold.distillation_pairs import PairsManifest, verify_pairs_manifest


EVAL_SCHEMA_VERSION = "cathedral.distillation_eval.v1"
SERVING_SCHEMA_VERSION = "cathedral.distillation_serving.v1"


class LeakageError(ValueError):
    """Raised when eval is attempted on a non-test split."""


@dataclass(frozen=True)
class EvalConfig:
    min_accuracy: float = 0.6
    min_positive_recall: float = 0.5
    must_beat_baseline: bool = True


@dataclass(frozen=True)
class EvalReport:
    n: int
    accuracy: float
    per_category: dict[str, dict[str, float]]
    positive_recall: float
    baseline_accuracy: float
    beats_baseline: bool
    model_artifact_sha256: str  # binds the report to the model that produced it
    model_corpus_hash: str
    test_pairs_hash: str  # binds the report to the exact test split scored
    eval_hash: str

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": EVAL_SCHEMA_VERSION,
            "n": self.n,
            "accuracy": self.accuracy,
            "per_category": self.per_category,
            "positive_recall": self.positive_recall,
            "baseline_accuracy": self.baseline_accuracy,
            "beats_baseline": self.beats_baseline,
            "model_artifact_sha256": self.model_artifact_sha256,
            "model_corpus_hash": self.model_corpus_hash,
            "test_pairs_hash": self.test_pairs_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "eval_hash": self.eval_hash}


@dataclass(frozen=True)
class ServeConfig:
    eval: EvalConfig = field(default_factory=EvalConfig)
    receipt_max_age_seconds: int = 3600


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash_obj(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _label_is_positive(label: str) -> bool:
    return label == "accepted_reproduced_witness"


def evaluate(
    artifact: Any,
    pairs_manifest: PairsManifest,
    *,
    corpus: Corpus | None = None,
    predict: Callable[[str], str] | None = None,
    config: EvalConfig | None = None,
) -> EvalReport:
    """Score a model on the held-out TEST split only.

    ``predict`` maps a pair input -> predicted label. If omitted, a deterministic
    stub predicts the majority label (used only to exercise the gate offline).
    When ``corpus`` is provided, the test manifest is verified against it so a
    relabel-and-rehash split swap cannot pass training pairs off as held-out.
    """
    if pairs_manifest.split != "test":
        raise LeakageError(f"eval_requires_test_split:{pairs_manifest.split}")
    # Verify the test manifest against the trusted corpus (authenticity), not
    # just internal consistency.
    verify_pairs_manifest(pairs_manifest, corpus)
    # A usable eval requires a real, provenanced model artifact whose corpus
    # matches the test split's corpus (no partial/unprovenanced artifacts).
    if artifact is None:
        raise LeakageError("eval_requires_model_artifact")
    model_sha = str(getattr(artifact, "artifact_sha256", ""))
    model_corpus = str(getattr(artifact, "corpus_hash", ""))
    if not model_sha or not model_corpus:
        raise LeakageError("eval_requires_provenanced_artifact")
    if pairs_manifest.corpus_hash != model_corpus:
        raise LeakageError("eval_corpus_hash_mismatch_with_model")
    config = config or EvalConfig()
    pairs = pairs_manifest.pairs
    n = len(pairs)
    if n == 0:
        raise ValueError("eval_requires_nonempty_test_split")

    truth = [p.label for p in pairs]
    pos_labels = [t for t in truth if _label_is_positive(t)]
    # Deterministic majority label: sort labels so ties don't depend on set
    # iteration order (finding #9), which would make eval_hash nondeterministic.
    majority_label = max(sorted(set(truth)), key=truth.count)
    baseline_correct = sum(1 for t in truth if t == majority_label)
    baseline_accuracy = baseline_correct / n

    if predict is None:
        # Degenerate stub: always predicts majority class. Used to prove the
        # gate rejects a degenerate classifier (it will not beat baseline).
        preds = [majority_label for _ in pairs]
    else:
        preds = [predict(p.input) for p in pairs]

    correct = sum(1 for pred, t in zip(preds, truth) if pred == t)
    accuracy = correct / n

    # Positive-class recall.
    if pos_labels:
        pos_hits = sum(
            1 for pred, t in zip(preds, truth)
            if _label_is_positive(t) and pred == t
        )
        positive_recall = pos_hits / len(pos_labels)
    else:
        positive_recall = 0.0

    per_category = _per_category_metrics(preds, truth)
    beats_baseline = accuracy > baseline_accuracy

    # Bind the report to the model + test split it scored (finding #4).
    report_body = {
        "schema_version": EVAL_SCHEMA_VERSION,
        "n": n,
        "accuracy": accuracy,
        "per_category": per_category,
        "positive_recall": positive_recall,
        "baseline_accuracy": baseline_accuracy,
        "beats_baseline": beats_baseline,
        "model_artifact_sha256": str(getattr(artifact, "artifact_sha256", "")),
        "model_corpus_hash": str(getattr(artifact, "corpus_hash", "")),
        "test_pairs_hash": pairs_manifest.pairs_hash,
    }
    return EvalReport(
        n=n,
        accuracy=accuracy,
        per_category=per_category,
        positive_recall=positive_recall,
        baseline_accuracy=baseline_accuracy,
        beats_baseline=beats_baseline,
        model_artifact_sha256=report_body["model_artifact_sha256"],
        model_corpus_hash=report_body["model_corpus_hash"],
        test_pairs_hash=report_body["test_pairs_hash"],
        eval_hash=_hash_obj(report_body),
    )


def _per_category_metrics(preds: list[str], truth: list[str]) -> dict[str, dict[str, float]]:
    labels = set(truth) | set(preds)
    out: dict[str, dict[str, float]] = {}
    for label in labels:
        tp = sum(1 for pr, t in zip(preds, truth) if pr == label and t == label)
        fp = sum(1 for pr, t in zip(preds, truth) if pr == label and t != label)
        fn = sum(1 for pr, t in zip(preds, truth) if pr != label and t == label)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        out[label] = {"precision": precision, "recall": recall, "f1": f1}
    return out


def _eval_report_authentic(
    report: EvalReport, artifact: Any, test_pairs_manifest: PairsManifest | None = None
) -> bool:
    """Recompute eval_hash and require the report binds to a real artifact and,
    when supplied, to the trusted test manifest.

    No None bypass: a serving decision that could lead to earning must have a
    concrete model artifact whose identity matches the report.
    """
    if _hash_obj(report.body()) != report.eval_hash:
        return False
    if artifact is None:
        return False
    expected_sha = str(getattr(artifact, "artifact_sha256", ""))
    if not expected_sha or report.model_artifact_sha256 != expected_sha:
        return False
    # Bind to the trusted test split: a forged report cannot fake the real
    # test_pairs_hash if we require it to match the trusted manifest.
    if test_pairs_manifest is not None:
        if report.test_pairs_hash != test_pairs_manifest.pairs_hash:
            return False
    expected_corpus = str(getattr(artifact, "corpus_hash", ""))
    if not expected_corpus or report.model_corpus_hash != expected_corpus:
        return False
    return True


def _eval_passes(report: EvalReport, cfg: EvalConfig) -> bool:
    if report.accuracy < cfg.min_accuracy:
        return False
    if report.positive_recall < cfg.min_positive_recall:
        return False
    if cfg.must_beat_baseline and not report.beats_baseline:
        return False
    return True


def serving_manifest(
    artifact: Any,
    eval_report: EvalReport | None,
    *,
    test_pairs_manifest: PairsManifest | None = None,
    corpus: Corpus | None = None,
    predict: Callable[[str], str] | None = None,
    config: ServeConfig | None = None,
    gated: bool = True,
    deployment_id: str | None = None,
    auth: str | None = None,
    health_receipt: dict[str, Any] | None = None,
    usage_receipt: dict[str, Any] | None = None,
    now: int = 0,
) -> dict[str, Any]:
    """Build a serving manifest, advancing state only when evidence exists.

    Reaching ``earning`` requires a TRUSTED evaluation: the caller must supply the
    trusted ``corpus``, ``test_pairs_manifest``, and a ``predict`` function, and
    this function RE-RUNS ``evaluate`` itself. Caller-supplied ``eval_report``
    metrics are never trusted for earning (they can be forged); at most a
    self-consistent report lets the manifest report ``ready`` without earning.
    """
    config = config or ServeConfig()
    state = "unevaluated"
    receipt_hash = ""
    receipt_source = ""
    trusted_eval = False

    # Trusted path: recompute the eval from the trusted inputs. This is the ONLY
    # way to reach earning — forged metrics cannot survive re-evaluation.
    if corpus is not None and test_pairs_manifest is not None and predict is not None:
        try:
            eval_report = evaluate(
                artifact, test_pairs_manifest, corpus=corpus,
                predict=predict, config=config.eval,
            )
            trusted_eval = True
        except Exception:
            eval_report = None
    elif eval_report is not None:
        # Untrusted path: a self-consistent report can inform `ready`, but can
        # never reach `earning` (guarded below).
        if not _eval_report_authentic(eval_report, artifact, test_pairs_manifest):
            eval_report = None

    if eval_report is not None:
        state = "evaluated"
        if _eval_passes(eval_report, config.eval):
            state = "ready"
            if deployment_id and auth:
                state = "deployed"
                if _receipt_fresh(health_receipt, config, now):
                    state = "healthy"
                    # earning requires a TRUSTED (re-run) eval, not a caller report.
                    if (
                        trusted_eval
                        and _receipt_fresh(usage_receipt, config, now)
                        and _receipt_signed(usage_receipt)
                    ):
                        state = "earning"
                        receipt_hash = str(usage_receipt.get("receipt_hash"))
                        receipt_source = str(usage_receipt.get("receipt_source"))

    # Stale demotion: a deployment that HAS receipts which have aged out is
    # "stale" (distinct from a fresh deployment that never had receipts yet).
    if state == "deployed":
        health_present = isinstance(health_receipt, dict)
        usage_present = isinstance(usage_receipt, dict)
        health_stale = health_present and not _receipt_fresh(health_receipt, config, now)
        usage_stale = usage_present and not _receipt_fresh(usage_receipt, config, now)
        if health_stale or usage_stale:
            state = "stale"
    elif state in ("healthy", "earning"):
        # Demote if EITHER receipt is stale. A stale usage receipt with a fresh
        # health receipt must not remain "healthy" (finding: usage staleness
        # must demote too).
        if not _receipt_fresh(health_receipt, config, now):
            state = "stale"
        elif isinstance(usage_receipt, dict) and not _receipt_fresh(usage_receipt, config, now):
            state = "stale"

    return {
        "schema_version": SERVING_SCHEMA_VERSION,
        "state": state,
        "gated": bool(gated),
        "deployment_id": deployment_id or "",
        "auth": auth or "",
        "eval": eval_report.to_dict() if eval_report else None,
        "health_receipt": health_receipt,
        "usage_receipt": usage_receipt,
        "receipt_hash": receipt_hash,
        "receipt_source": receipt_source,
        "updated_at": now,
        "earning": state == "earning",
        "ready": state in ("ready", "deployed", "healthy", "earning"),
    }


def _receipt_fresh(receipt: dict[str, Any] | None, config: ServeConfig, now: int) -> bool:
    if not isinstance(receipt, dict):
        return False
    ts = receipt.get("timestamp")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return False
    age = now - ts
    # Reject future-dated receipts (age < 0) as well as stale ones (finding #5).
    return 0 <= age <= config.receipt_max_age_seconds


def _receipt_signed(receipt: dict[str, Any] | None) -> bool:
    if not isinstance(receipt, dict):
        return False
    return bool(receipt.get("receipt_hash")) and bool(receipt.get("receipt_source"))
