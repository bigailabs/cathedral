"""Full-provenance audit for the two-mode validator.

Wraps the ``cathedral`` package (installed via the ``provenance`` extra) to:

  * fetch the signed evidence index, epoch manifest, and content-addressed
    artifacts from the public evidence surface;
  * independently verify the policy registry, the signed score-class report,
    and every referenced assurance receipt;
  * deterministically recompute the pre-burn weight vector under the pinned
    versioned reward mechanism; and
  * compare the recomputation against Cathedral's signed vector.

Anti-equivocation state lives in the validator's state file: a report that
re-signs the same source epoch with different contents, or moves the source
epoch backwards, fails the audit outright.

If the ``cathedral`` package is not installed the audit reports NOT_PROVEN
(shadow mode warns loudly; authority mode refuses to submit).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

MECHANISM_DEFAULT = "validated_supply_v1"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ProvenanceUnavailable(Exception):
    """Provenance cannot run here (package missing or pins not configured).

    Reported as NOT_PROVEN: nothing is known to be wrong, but nothing was
    independently proven either. Authority mode refuses to submit on it.
    """

    def __init__(self, message: str, remediation: str) -> None:
        super().__init__(message)
        self.remediation = remediation


class ProvenanceAuditError(Exception):
    """The audit failed a verification, equivocation, or transport gate."""


@dataclass(frozen=True)
class ProvenanceSettings:
    mode: str  # off | shadow | authority
    evidence_url: str | None = None
    evidence_dir: str | None = None
    registry_keys: str | None = None
    registry_keys_digest: str | None = None
    report_keys: str | None = None
    report_keys_digest: str | None = None
    index_keys: str | None = None
    index_keys_digest: str | None = None
    verifier_digest: str | None = None
    mechanism: str = MECHANISM_DEFAULT
    index_max_age_secs: float = 3600.0
    # Fail closed on a registry published more than 24 hours ago. Freshness
    # is restored by same-policy reissues at higher releases, never by
    # widening this ceiling.
    registry_max_age_secs: int = 86400

    def validate_for_audit(self) -> None:
        if self.mode not in ("off", "shadow", "authority"):
            raise ProvenanceAuditError(f"unknown provenance mode {self.mode!r}")
        if self.mode == "off":
            return
        missing = [
            f"--provenance-{name.replace('_', '-')}"
            for name in ("registry_keys", "report_keys", "index_keys", "verifier_digest")
            if not getattr(self, name)
        ]
        if not (self.evidence_url or self.evidence_dir):
            missing.insert(0, "--evidence-url")
        if missing:
            raise ProvenanceUnavailable(
                "provenance pins are not configured: missing " + ", ".join(missing),
                "Configure the trusted key files and verifier digest from "
                "VALIDATOR.md (or set --provenance off to silence this).",
            )


@dataclass
class ProvenanceAudit:
    status: str  # PASS | FAIL | NOT_PROVEN
    source_epoch: int | None = None
    report_id: str | None = None
    previous_report_id: str | None = None
    manifest_digest: str | None = None
    policy_release: int | None = None
    mechanism: str | None = None
    recomputed: dict[str, float] = field(default_factory=dict)
    agrees_with_vector: bool | None = None
    discrepancies: list[str] = field(default_factory=list)
    receipt_hotkeys: list[str] = field(default_factory=list)
    duration_ms: float | None = None
    error: str | None = None
    remediation: str | None = None


def _load_pubkeys(path: str, pinned_digest: str | None, label: str) -> dict[str, bytes]:
    raw = Path(path).read_bytes()
    if len(raw) > 65536:
        raise ProvenanceAuditError(f"{label} file is unreasonably large")
    if pinned_digest is not None:
        if _DIGEST_RE.fullmatch(pinned_digest) is None:
            raise ProvenanceAuditError(f"{label} digest pin is malformed")
        actual = "sha256:" + hashlib.sha256(raw).hexdigest()
        if actual != pinned_digest:
            raise ProvenanceAuditError(f"{label} file does not match its digest pin")
    try:
        document = json.loads(raw)
        keys = {}
        for key_id, encoded in document.items():
            decoded = base64.b64decode(encoded, validate=True)
            if not isinstance(key_id, str) or len(decoded) != 32:
                raise ValueError
            keys[key_id] = decoded
    except (ValueError, TypeError, AttributeError, binascii.Error) as exc:
        raise ProvenanceAuditError(
            f"{label} file must map key ids to 32-byte base64 keys"
        ) from exc
    if not keys:
        raise ProvenanceAuditError(f"{label} file is empty")
    return keys


def _fetcher(settings: ProvenanceSettings):
    if settings.evidence_dir:
        root = Path(settings.evidence_dir)

        def load_index() -> bytes:
            path = root / "index.json"
            if not path.exists():
                raise ProvenanceAuditError("evidence index is missing from the store")
            return path.read_bytes()

        def load_blob(digest: str) -> bytes:
            if _DIGEST_RE.fullmatch(digest) is None:
                raise ProvenanceAuditError(f"malformed blob digest {digest!r}")
            path = root / "blobs" / "sha256" / digest.split(":", 1)[1]
            if not path.exists():
                raise ProvenanceAuditError(f"blob {digest} is not in the store")
            data = path.read_bytes()
            if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
                raise ProvenanceAuditError(f"blob {digest} content is corrupt")
            return data

        return load_index, load_blob

    base = (settings.evidence_url or "").rstrip("/")
    if not base.startswith("https://"):
        raise ProvenanceAuditError("evidence URL must be https")

    def fetch(path: str) -> bytes:
        request = urllib.request.Request(
            base + path, headers={"User-Agent": "cathedral-two-mode-validator/1.0"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()

    def load_index() -> bytes:
        return fetch("/index.json")

    def load_blob(digest: str) -> bytes:
        if _DIGEST_RE.fullmatch(digest) is None:
            raise ProvenanceAuditError(f"malformed blob digest {digest!r}")
        data = fetch("/blobs/sha256/" + digest.split(":", 1)[1])
        if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            raise ProvenanceAuditError(f"fetched blob does not match digest {digest}")
        return data

    return load_index, load_blob


def check_chain_state(
    audit: ProvenanceAudit, state: Mapping[str, Any]
) -> None:
    """Fail on source-epoch rollback or same-epoch report equivocation."""
    last_epoch = state.get("provenance_last_source_epoch")
    last_report = state.get("provenance_last_report_id")
    if last_epoch is None or audit.source_epoch is None:
        return
    if audit.source_epoch < int(last_epoch):
        raise ProvenanceAuditError(
            f"evidence rollback: source epoch {audit.source_epoch} < "
            f"last audited {last_epoch}"
        )
    if audit.source_epoch == int(last_epoch) and audit.report_id != last_report:
        raise ProvenanceAuditError(
            f"report equivocation: source epoch {audit.source_epoch} was already "
            f"audited with a different report id"
        )


def run_audit(
    settings: ProvenanceSettings,
    *,
    network: str,
    netuid: int,
    vector_payload: Mapping[str, Any] | None,
    state: Mapping[str, Any],
) -> ProvenanceAudit:
    """Run one full-provenance audit. Never raises; the status carries the verdict."""
    started = time.monotonic()
    try:
        settings.validate_for_audit()
        try:
            from cathedral import provenance
            from cathedral.evidence import parse_manifest, verify_index
        except ImportError as exc:
            raise ProvenanceUnavailable(
                "the cathedral provenance package is not installed",
                "pip install 'cathedralsubnet[provenance]' (or pip install "
                "'cathedral @ git+https://github.com/cathedralai/"
                "cathedralconfidential.git')",
            ) from exc

        load_index, load_blob = _fetcher(settings)
        index_keys = _load_pubkeys(
            settings.index_keys, settings.index_keys_digest, "index keys"
        )
        index_document = verify_index(
            load_index(),
            index_keys,
            expected_network=network,
            expected_netuid=netuid,
            max_age_seconds=settings.index_max_age_secs,
        )
        manifest_digest = index_document["latest"]["manifest"]
        manifest = parse_manifest(load_blob(manifest_digest))
        if manifest["network"] != network or manifest["netuid"] != netuid:
            raise ProvenanceAuditError("evidence manifest network/netuid mismatch")
        if manifest["reward_mechanism"]["id"] != settings.mechanism:
            raise ProvenanceAuditError(
                f"manifest mechanism {manifest['reward_mechanism']['id']!r} does not "
                f"match the pinned mechanism {settings.mechanism!r}"
            )
        if manifest["verifier"]["digest"] != settings.verifier_digest:
            raise ProvenanceAuditError(
                "manifest verifier digest does not match the pinned verifier"
            )

        registry_keys = _load_pubkeys(
            settings.registry_keys, settings.registry_keys_digest, "registry keys"
        )
        report_keys = _load_pubkeys(
            settings.report_keys, settings.report_keys_digest, "report keys"
        )
        registry_bytes = load_blob(manifest["policy_registry"]["blob"])
        report_bytes = load_blob(manifest["score_report"]["blob"])
        receipts_by_id = {
            row["receipt_id"]: load_blob(row["blob"]) for row in manifest["receipts"]
        }

        result = provenance.verify_and_recompute(
            report_bytes=report_bytes,
            receipts_by_id=receipts_by_id,
            registry_bytes=registry_bytes,
            trusted_registry_keys=registry_keys,
            report_signing_keys=report_keys,
            expected_network=network,
            expected_netuid=netuid,
            expected_verifier_digest=settings.verifier_digest,
            mechanism_id=settings.mechanism,
            registry_max_age_seconds=settings.registry_max_age_secs,
        )
        if result.policy_release != manifest["policy_registry"]["release"]:
            raise ProvenanceAuditError(
                "verified registry release differs from the manifest"
            )
        if result.report_id != manifest["score_report"]["report_id"]:
            raise ProvenanceAuditError("verified report id differs from the manifest")

        audit = ProvenanceAudit(
            status="PASS",
            source_epoch=result.source_epoch,
            report_id=result.report_id,
            previous_report_id=result.previous_report_id,
            manifest_digest=manifest_digest,
            policy_release=result.policy_release,
            mechanism=result.mechanism_id,
            recomputed=dict(result.recomputed_hotkey_weights),
            receipt_hotkeys=[
                miner.hotkey for miner in result.miners if miner.receipt_verified
            ],
        )
        check_chain_state(audit, state)

        if vector_payload is not None:
            agree, discrepancies = provenance.compare_with_vector(
                result, vector_payload
            )
            # A disagreement is NOT a chain-verification failure: the evidence
            # verified and the recomputation stands. status stays PASS; the
            # caller decides what to submit and how loudly to report.
            audit.agrees_with_vector = agree
            audit.discrepancies = discrepancies
            if not agree:
                audit.remediation = (
                    "Escalate to Cathedral operators. Shadow mode keeps submitting "
                    "the signed vector; authority mode submits the recomputed one."
                )
        audit.duration_ms = (time.monotonic() - started) * 1000
        return audit
    except ProvenanceUnavailable as exc:
        return ProvenanceAudit(
            status="NOT_PROVEN",
            error=str(exc),
            remediation=exc.remediation,
            duration_ms=(time.monotonic() - started) * 1000,
        )
    except Exception as exc:  # noqa: BLE001 - audits never crash the validator
        return ProvenanceAudit(
            status="FAIL",
            error=f"{type(exc).__name__}: {exc}"[:512],
            remediation=(
                "Check the evidence endpoint, pinned keys, and digests. "
                "Authority mode will not submit until the audit passes."
            ),
            duration_ms=(time.monotonic() - started) * 1000,
        )
