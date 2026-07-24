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
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    # FULL assurance inputs: the controlled-disclosure envelope directory and
    # a local verifier binary whose bytes must match the manifest's blob pin
    # AND reproduce the operator-pinned implementation digest. Without them
    # the audit is receipts-only (NOT_PROVEN for authority purposes).
    controlled_dir: str | None = None
    verifier_binary: str | None = None
    source_revision: str | None = None
    # Testing only: permit evidence hosts that resolve to private ranges.
    allow_private_hosts: bool = False
    # One whole-audit wall-clock budget (DNS, connect, TLS, every blob).
    audit_deadline_secs: float = 120.0
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
            for name in (
                "registry_keys",
                "report_keys",
                "index_keys",
                "verifier_digest",
            )
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
        if self.mode == "authority":
            if self.allow_private_hosts:
                raise ProvenanceAuditError(
                    "allow_private_hosts is testing-only and is refused in "
                    "authority mode (SSRF policy)"
                )
            # Authority has NO optional security pins: every immutable pin
            # and the raw-evidence source are mandatory.
            required = [
                ("registry_keys_digest", "--provenance-registry-keys-digest"),
                ("report_keys_digest", "--provenance-report-keys-digest"),
                ("index_keys_digest", "--provenance-index-keys-digest"),
                ("source_revision", "--provenance-source-revision"),
                ("verifier_binary", "--provenance-verifier-binary"),
                ("controlled_dir", "--provenance-controlled-dir"),
            ]
            absent = [flag for name, flag in required if not getattr(self, name)]
            if absent:
                raise ProvenanceAuditError(
                    "authority mode requires immutable pins and a raw-evidence "
                    "source: missing " + ", ".join(absent)
                )


@dataclass
class ProvenanceAudit:
    status: str  # PASS | FAIL | NOT_PROVEN
    assurance: str = "receipts_only"  # full | receipts_only
    index_source_epoch: int | None = None
    index_manifest: str | None = None
    policy_digest: str | None = None
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


RESOLVER_SLOT_CAP = 16
_RESOLVER_SLOTS = None
_RESOLVER_SLOTS_GUARD = None


def _resolver_slots():
    """Process-global bounded resolver slot pool (defect 5): every in-flight
    getaddrinfo — including abandoned timed-out calls — holds one slot until
    the resolver thread actually returns, so repeated timeouts can never
    accumulate unbounded daemon threads; exhaustion fails promptly."""
    global _RESOLVER_SLOTS, _RESOLVER_SLOTS_GUARD
    import threading as threading_module

    if _RESOLVER_SLOTS_GUARD is None:
        _RESOLVER_SLOTS_GUARD = threading_module.Lock()
    with _RESOLVER_SLOTS_GUARD:
        if _RESOLVER_SLOTS is None:
            _RESOLVER_SLOTS = threading_module.BoundedSemaphore(RESOLVER_SLOT_CAP)
    return _RESOLVER_SLOTS


def _getaddrinfo_bounded(host: str, port: int, timeout: float) -> list:
    """Resolve on a daemon thread from the bounded slot pool, waiting at
    most ``timeout`` seconds. An abandoned slow call retains its slot only
    until the resolver returns; a full pool fails promptly."""
    import queue as queue_module
    import socket
    import threading as threading_module

    slots = _resolver_slots()
    if not slots.acquire(timeout=max(0.0, min(timeout, 5.0))):
        raise ProvenanceAuditError(
            f"DNS resolver capacity exhausted while resolving {host}: "
            f"{RESOLVER_SLOT_CAP} lookups are already in flight"
        )
    channel: queue_module.Queue = queue_module.Queue(maxsize=1)

    def _resolve() -> None:
        try:
            try:
                channel.put(
                    ("ok", socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP))
                )
            except OSError as exc:
                channel.put(("err", exc))
        finally:
            slots.release()

    try:
        threading_module.Thread(
            target=_resolve, name="cathedral-dns", daemon=True
        ).start()
    except BaseException:
        slots.release()
        raise
    try:
        kind, value = channel.get(timeout=max(0.0, timeout))
    except queue_module.Empty:
        raise ProvenanceAuditError(
            f"DNS resolution for {host} exceeded the audit deadline"
        ) from None
    if kind == "err":
        raise ProvenanceAuditError(f"evidence host does not resolve: {host}") from value
    return value


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
    import http.client
    import ipaddress
    import socket
    import ssl
    import urllib.parse

    parsed = urllib.parse.urlsplit(base)
    if parsed.username or parsed.password:
        raise ProvenanceAuditError("evidence URL must be credential-free")
    host = parsed.hostname or ""
    if not host:
        raise ProvenanceAuditError("evidence URL has no host")
    infos = _getaddrinfo_bounded(
        host, parsed.port or 443, min(30.0, settings.audit_deadline_secs)
    )
    if not infos:
        raise ProvenanceAuditError(f"evidence host does not resolve: {host}")
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not settings.allow_private_hosts and (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise ProvenanceAuditError(
                f"evidence host resolves to a non-public address: {host}"
            )
    peer_ip = infos[0][4][0]
    peer_port = parsed.port or 443
    base_path = parsed.path.rstrip("/")

    MAX_FETCH_BYTES = 4 * 1024 * 1024
    # Whole-audit caps: one monotonic deadline plus aggregate byte and
    # artifact limits shared by EVERY remote operation in this audit.
    audit_deadline = time.monotonic() + settings.audit_deadline_secs
    remaining = {"bytes": 64 * 1024 * 1024, "artifacts": 256}

    def _check_budget() -> float:
        seconds_left = audit_deadline - time.monotonic()
        if seconds_left <= 0:
            raise ProvenanceAuditError("audit exceeded its total deadline")
        return seconds_left

    class _PinnedConnection(http.client.HTTPSConnection):
        def connect(self) -> None:
            raw = socket.create_connection((peer_ip, peer_port), self.timeout)
            self.sock = self._context.wrap_socket(raw, server_hostname=host)

    def fetch(path: str, timeout: float = 30.0) -> bytes:
        remaining["artifacts"] -= 1
        if remaining["artifacts"] < 0:
            raise ProvenanceAuditError("audit exceeded its artifact cap")
        timeout = min(timeout, _check_budget())
        connection = _PinnedConnection(
            host, peer_port, timeout=timeout, context=ssl.create_default_context()
        )
        try:
            connection.request(
                "GET",
                base_path + path,
                headers={
                    "Host": host,
                    "User-Agent": "cathedral-two-mode-validator/1.0",
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                raise ProvenanceAuditError(
                    f"evidence fetch failed with status {response.status} "
                    "(redirects are never followed)"
                )
            chunks: list[bytes] = []
            received = 0
            while True:
                _check_budget()
                chunk = response.read(min(65536, MAX_FETCH_BYTES + 1 - received))
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_FETCH_BYTES:
                    raise ProvenanceAuditError(
                        "evidence response exceeds the bounded limit"
                    )
                remaining["bytes"] -= len(chunk)
                if remaining["bytes"] < 0:
                    raise ProvenanceAuditError("audit exceeded its aggregate byte cap")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            connection.close()

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


def check_chain_state(audit: ProvenanceAudit, state: Mapping[str, Any]) -> None:
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
    current_block: int | None = None,
    historical_hotkeys_lookup=None,
    block_hash_lookup=None,
) -> ProvenanceAudit:
    """Run one full-provenance audit. Never raises; the status carries the verdict."""
    started = time.monotonic()
    audit_deadline = started + settings.audit_deadline_secs
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
        index_epoch = int(index_document["latest"]["source_epoch"])
        # Durable anti-rollback fences for the SIGNED INDEX itself: an older
        # signed index, or the same epoch re-signed with a different
        # manifest, must never verify.
        last_index_epoch = state.get("provenance_index_epoch")
        last_index_manifest = state.get("provenance_index_manifest")
        if isinstance(last_index_epoch, int):
            if index_epoch < last_index_epoch:
                raise ProvenanceAuditError(
                    f"index rollback: latest epoch {index_epoch} < recorded "
                    f"high-water {last_index_epoch}"
                )
            if index_epoch == last_index_epoch and (
                manifest_digest != last_index_manifest
            ):
                raise ProvenanceAuditError(
                    "index equivocation: same epoch, different manifest"
                )
        manifest = parse_manifest(load_blob(manifest_digest))
        if manifest["network"] != network or manifest["netuid"] != netuid:
            raise ProvenanceAuditError("evidence manifest network/netuid mismatch")
        if int(manifest["source_epoch"]) != index_epoch:
            raise ProvenanceAuditError(
                "index latest.source_epoch does not match the manifest it points to"
            )
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
        # Durable policy fences: a lower registry release, or the same
        # release with a different digest, must never verify again.
        last_policy_release = state.get("provenance_policy_release")
        last_policy_digest = state.get("provenance_policy_digest")
        manifest_release = int(manifest["policy_registry"]["release"])
        manifest_policy_digest = str(manifest["policy_registry"]["digest"])
        if isinstance(last_policy_release, int):
            if manifest_release < last_policy_release:
                raise ProvenanceAuditError(
                    f"policy rollback: release {manifest_release} < recorded "
                    f"high-water {last_policy_release}"
                )
            if (
                manifest_release == last_policy_release
                and manifest_policy_digest != last_policy_digest
            ):
                raise ProvenanceAuditError(
                    "policy equivocation: same release, different digest"
                )
        registry_bytes = load_blob(manifest["policy_registry"]["blob"])
        report_bytes = load_blob(manifest["score_report"]["blob"])
        receipts_by_id = {
            row["receipt_id"]: load_blob(row["blob"]) for row in manifest["receipts"]
        }
        work_artifacts_by_receipt = {
            row["receipt_id"]: (
                load_blob(row["work_item_blob"]),
                load_blob(row["result_blob"]),
            )
            for row in manifest["receipts"]
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
            candidate_set=manifest["candidate_set"],
            work_artifacts_by_receipt=work_artifacts_by_receipt,
            current_block=current_block,
        )
        # Report predecessor continuity is ALWAYS enforced across audits:
        # a new export must chain from the last audited report.
        last_report = state.get("provenance_last_report_id")
        last_epoch = state.get("provenance_last_source_epoch")
        if (
            isinstance(last_epoch, int)
            and result.source_epoch > last_epoch
            and last_report is not None
            and result.previous_report_id != last_report
        ):
            raise ProvenanceAuditError(
                "score report does not chain from the last audited report"
            )
        if result.policy_release != manifest["policy_registry"]["release"]:
            raise ProvenanceAuditError(
                "verified registry release differs from the manifest"
            )
        if result.report_id != manifest["score_report"]["report_id"]:
            raise ProvenanceAuditError("verified report id differs from the manifest")
        if settings.source_revision and (
            manifest["source_revision"] != settings.source_revision
        ):
            raise ProvenanceAuditError(
                "manifest source revision does not match the operator's pin"
            )

        if settings.controlled_dir:
            from pathlib import Path as _Path

            bindings = {row["hotkey"]: row for row in manifest["attestations"]}
            envelopes: dict[str, bytes] = {}
            for miner in result.miners:
                if not miner.receipt_verified:
                    continue
                binding = bindings.get(miner.hotkey) or {}
                envelope_digest = binding.get("envelope_digest")
                if not envelope_digest:
                    raise ProvenanceUnavailable(
                        f"no controlled envelope for {miner.hotkey!r}",
                        "request the controlled-disclosure package for this epoch",
                    )
                envelope_path = (
                    _Path(settings.controlled_dir)
                    / f"{str(envelope_digest).split(':', 1)[1]}.json"
                )
                if envelope_path.is_symlink() or not envelope_path.is_file():
                    raise ProvenanceUnavailable(
                        f"controlled envelope file missing for {miner.hotkey!r}",
                        "request the controlled-disclosure package for this epoch",
                    )
                envelopes[miner.hotkey] = envelope_path.read_bytes()
            verifier_info = manifest["verifier"]
            if not settings.verifier_binary:
                raise ProvenanceUnavailable(
                    "no verifier binary configured for full assurance",
                    "set --provenance-verifier-binary to the pinned verifier",
                )
            if not verifier_info.get("binary_blob") or not verifier_info.get("command"):
                raise ProvenanceAuditError(
                    "manifest lacks verifier binary/command bindings for full mode"
                )
            candidates = manifest["candidate_set"]["candidates"]
            active = [row for row in candidates if row["outcome"] != "retired"]
            all_rejected = bool(active) and all(
                row["outcome"] == "rejected" for row in active
            )
            candidate_snapshot = manifest["candidate_set"]
            # Independent HISTORICAL chain cross-checks (defect 1): full
            # assurance requires the manifest's candidate set to EXACTLY
            # equal the SN39 metagraph AT candidate_set.block, and the
            # anchored hash to equal get_block_hash(block). The current
            # metagraph proves nothing about the anchored epoch; a subset
            # check would still admit omission. Unavailable or malformed
            # history is NOT_PROVEN — never a silent pass.
            if historical_hotkeys_lookup is None or block_hash_lookup is None:
                raise ProvenanceUnavailable(
                    "historical chain lookups are unavailable for full assurance",
                    "provide chain access so the anchored block hash and the "
                    "historical metagraph at candidate_set.block can be "
                    "independently verified",
                )
            anchored_block = int(candidate_snapshot["block"])
            try:
                independent_hash = block_hash_lookup(anchored_block)
            except Exception as exc:
                raise ProvenanceUnavailable(
                    f"finalized block hash lookup failed for block {anchored_block}",
                    "restore archive-node access and re-run the audit",
                ) from exc
            if independent_hash is None:
                raise ProvenanceUnavailable(
                    f"the finalized hash of anchored block {anchored_block} "
                    "is unavailable",
                    "restore archive-node access and re-run the audit",
                )
            if str(independent_hash).lower().removeprefix("0x") != str(
                candidate_snapshot["block_hash"]
            ).lower().removeprefix("0x"):
                raise ProvenanceAuditError(
                    "anchored block hash does not match the independently queried chain"
                )
            try:
                historical = historical_hotkeys_lookup(anchored_block)
            except Exception as exc:
                raise ProvenanceUnavailable(
                    f"historical metagraph lookup failed for block {anchored_block}",
                    "restore archive-node access and re-run the audit",
                ) from exc
            if historical is None:
                raise ProvenanceUnavailable(
                    f"the historical metagraph at block {anchored_block} is "
                    "unavailable",
                    "restore archive-node access and re-run the audit",
                )
            historical_set = {str(h) for h in historical}
            if not historical_set or any(not h for h in historical_set):
                raise ProvenanceUnavailable(
                    "the historical metagraph lookup returned malformed hotkeys",
                    "restore archive-node access and re-run the audit",
                )
            manifest_set = {
                str(row["hotkey"]) for row in candidate_snapshot["candidates"]
            }
            extra = manifest_set - historical_set
            if extra:
                raise ProvenanceAuditError(
                    "manifest carries candidates not registered on the "
                    f"historical metagraph at block {anchored_block}: "
                    f"{sorted(extra)}"
                )
            omitted = historical_set - manifest_set
            if omitted:
                raise ProvenanceAuditError(
                    "manifest omits candidates registered on the historical "
                    f"metagraph at block {anchored_block}: {sorted(omitted)}"
                )
            result = provenance.replay_positive_miners(
                result,
                candidates_all_rejected=all_rejected,
                epoch_generated_at=manifest["generated_at"],
                deadline_monotonic=audit_deadline,
                challenge_anchor={
                    "block": anchored_block,
                    "block_hash": candidate_snapshot["block_hash"],
                    "network": network,
                    "netuid": netuid,
                },
                registry=provenance.load_registry(
                    registry_bytes,
                    registry_keys,
                    max_age_seconds=settings.registry_max_age_secs,
                ),
                envelopes_by_hotkey=envelopes,
                attestation_bindings=bindings,
                verifier_binary=_Path(settings.verifier_binary).read_bytes(),
                verifier_blob_digest=verifier_info["binary_blob"],
                verifier_command=tuple(verifier_info["command"]),
                verifier_artifacts=tuple(
                    verifier_info.get("artifacts") or verifier_info["command"]
                ),
            )

        audit = ProvenanceAudit(
            status="PASS",
            assurance=result.assurance_level,
            index_source_epoch=index_epoch,
            index_manifest=manifest_digest,
            policy_digest=result.policy_digest,
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
