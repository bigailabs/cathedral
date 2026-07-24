"""The thin v4 validator — the WHOLE validator, ~200 lines.

    fetch signed scores from the orchestrator
    -> verify Ed25519 signature against the pinned key
    -> sanity-check (finite, nonnegative, fresh, right subnet, no rollback)
    -> apply burn FROM THE SAME SIGNED PAYLOAD
    -> map hotkeys to uids against the live metagraph
    -> set weights

No local row database. No backfill. No rolling window. No score buckets.
Every scoring decision (recency, multi-lane composition, burn) lives
orchestrator-side and changes WITHOUT a validator release; this binary only
enforces that what it applies is exactly what the pinned key signed.

Run:  python -m scaffold.validator_thin --publisher-url https://api.cathedral.computer \
          --public-key-hex <pinned hex> [--once] [--broadcast]

Dry-run by default (computes + prints the uid vector, does not submit).
Rollback fence state persists in a small JSON file (--state-file), so a
publisher cannot re-serve an older policy_version after a restart.
"""
from __future__ import annotations

import argparse
import contextlib
import threading
import json
import math
import os
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import wire_vector as wire
from .chain import CHAIN_ENDPOINT_ENV, connection_target
from .events import FAIL, INFO, NOT_PROVEN, PASS, EventLogger
from .provenance_audit import (
    MECHANISM_DEFAULT,
    ProvenanceSettings,
    run_audit,
)


# Cathedral's published weight-policy signing key (kid: cathedral-weight-policy).
# This is a PUBLIC verification key — shipping it as the default means operators
# don't have to pin it by hand; the validator still applies only what this key
# signed. Verify it any time against
# https://api.cathedral.computer/.well-known/cathedral-jwks.json
# Override with --public-key-hex or CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY.
DEFAULT_PUBLIC_KEY_HEX = "10890a66aa752479cb3b634f366d7bd27c374324d83f88d2d6b69ab066f25e26"


def _ms_iso_now() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _lifecycle(event: str, detail: str = "") -> None:
    """Compact timestamped ASCII lifecycle event. No secrets.

    Format: ``<ts> <EVENT> <detail>`` — one line per state transition
    (VECTOR accepted/rejected, MAP complete, WEIGHTS dry-run, CHAIN
    submitted/failed).
    """
    line = f"{_ms_iso_now()} {event}"
    if detail:
        line += f" {detail}"
    print(line)


def _feed_label(publisher_url: str) -> str:
    """Return a log-safe feed identity without credentials, query, or fragment."""
    parsed = urlsplit(publisher_url)
    scheme = parsed.scheme or "https"
    host = parsed.hostname or "<invalid-host>"
    try:
        port = parsed.port
    except ValueError:
        port = None
    suffix = f":{port}" if port is not None else ""
    return f"{scheme}://{host}{suffix}"


MAX_VECTOR_FETCH_BYTES = 4 * 1024 * 1024


def fetch_vector(publisher_url: str, timeout: float = 30.0) -> dict[str, Any]:
    """Bounded HTTPS-only fetch: no redirects, no oversized bodies, and a
    strict JSON parse rejecting duplicate keys and non-finite numbers."""
    url = publisher_url.rstrip("/") + "/v1/validator/weights/next"
    if not url.startswith("https://"):
        raise wire.VectorError("publisher URL must be https")

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *arguments, **keywords):
            raise wire.VectorError("publisher must not redirect the vector fetch")

    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler())
    req = urllib.request.Request(
        url, headers={"User-Agent": "cathedral-thin-validator/1.0"})
    with opener.open(req, timeout=timeout) as resp:
        data = resp.read(MAX_VECTOR_FETCH_BYTES + 1)
    if len(data) > MAX_VECTOR_FETCH_BYTES:
        raise wire.VectorError("vector response exceeds the bounded size limit")

    def _no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise wire.VectorError("vector JSON has duplicate keys")
            result[key] = value
        return result

    document = json.loads(
        data.decode("utf-8"),
        object_pairs_hook=_no_duplicates,
        parse_constant=lambda _v: (_ for _ in ()).throw(
            wire.VectorError("vector JSON has non-finite numbers")
        ),
    )
    if not isinstance(document, dict):
        raise wire.VectorError("vector payload is not a JSON object")
    return document


# -- rollback fence ------------------------------------------------------------

def _read_state(state_file: Path) -> dict[str, Any]:
    """Read the whole durable state document (fence + provenance chain).

    FAIL CLOSED on symlinks and non-regular files: silently following a
    planted link could reopen the rollback window."""
    if not os.path.lexists(state_file):
        return {}
    if state_file.is_symlink() or not state_file.is_file():
        raise ValueError("validator state file must be a regular non-symlink file")
    document = json.loads(state_file.read_text())
    if not isinstance(document, dict):
        raise ValueError("validator state file is corrupt")
    return document


def _write_state(state_file: Path, updates: dict[str, Any]) -> None:
    """Locked atomic read-merge-write (0600, fsync, parent fsync) so the
    fence writer and the background shadow auditor never clobber each other
    and a crash mid-write can't corrupt the fail-closed load."""
    import fcntl

    state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_file.with_suffix(".lock")
    lock_descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        document = _read_state(state_file)
        document.update(updates)
        tmp = state_file.with_suffix(".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(tmp, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(document, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, state_file)
        parent = os.open(state_file.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        os.close(lock_descriptor)


def load_fence(state_file: Path) -> int:
    """FAIL CLOSED: only a genuinely absent state file means 'no fence yet'.
    A corrupt/unreadable file raises (the tick fails) instead of silently
    resetting the fence to -1 and reopening the rollback window."""
    document = _read_state(state_file)
    if "last_accepted_policy_version" not in document:
        return -1
    return int(document["last_accepted_policy_version"])


def save_fence(state_file: Path, version: int, vector_id: str) -> None:
    _write_state(state_file, {
        "last_accepted_policy_version": version,
        "last_vector_id": vector_id,
        "accepted_at": _ms_iso_now(),
    })


# -- two-mode provenance --------------------------------------------------------

def _provenance_settings(args) -> ProvenanceSettings:
    mode = getattr(args, "provenance", "shadow") or "shadow"
    evidence_url = getattr(args, "evidence_url", None)
    evidence_dir = getattr(args, "evidence_dir", None)
    if mode != "off" and not evidence_url and not evidence_dir:
        evidence_url = args.publisher_url.rstrip("/") + "/v1/evidence"
    return ProvenanceSettings(
        mode=mode,
        evidence_url=evidence_url,
        evidence_dir=evidence_dir,
        registry_keys=getattr(args, "provenance_registry_keys", None),
        registry_keys_digest=getattr(args, "provenance_registry_keys_digest", None),
        report_keys=getattr(args, "provenance_report_keys", None),
        report_keys_digest=getattr(args, "provenance_report_keys_digest", None),
        index_keys=getattr(args, "provenance_index_keys", None),
        index_keys_digest=getattr(args, "provenance_index_keys_digest", None),
        verifier_digest=getattr(args, "provenance_verifier_digest", None),
        mechanism=getattr(args, "provenance_mechanism", MECHANISM_DEFAULT)
        or MECHANISM_DEFAULT,
        controlled_dir=getattr(args, "provenance_controlled_dir", None),
        verifier_binary=getattr(args, "provenance_verifier_binary", None),
        source_revision=getattr(args, "provenance_source_revision", None),
        index_max_age_secs=float(getattr(args, "provenance_index_max_age_secs", 3600.0)),
    )


def _get_events(args) -> EventLogger:
    """One logger per process; authority is stamped on every record."""
    existing = getattr(args, "_events", None)
    if existing is not None:
        return existing
    mode = getattr(args, "provenance", "shadow") or "shadow"
    authority = "full_provenance" if mode == "authority" else "thin"
    logger = EventLogger(
        mode=authority,
        jsonl_path=getattr(args, "jsonl", None) or None,
        tty=sys.stdout,
    )
    try:
        args._events = logger
    except AttributeError:  # frozen namespaces in tests
        pass
    return logger


# The versioned mechanism fixes the burn fraction; the burn DESTINATION is
# the operator's configured pin resolved against the live metagraph. The
# signed Cathedral vector's burn row is comparison input only — authority
# mode never derives allocation from it.
MECHANISM_BURN_FRACTION = {MECHANISM_DEFAULT: 0.10}


def _provenance_uid_weights(
    recomputed: dict[str, float],
    *,
    mechanism: str,
    burn_hotkey: str,
    hotkey_to_uid: dict[str, int],
) -> dict[int, float]:
    """Authority mode: the COMPLETE UID vector from OUR recomputation.

    Inputs are the pinned versioned mechanism's shares, the operator's
    configured burn hotkey, and the live chain metagraph — nothing from
    Cathedral's signed vector. All-or-nothing mapping; nonfinite, negative,
    duplicate, or unmappable weights reject the whole vector.
    """
    burn_fraction = MECHANISM_BURN_FRACTION.get(mechanism)
    if burn_fraction is None:
        raise wire.VectorError(f"mechanism {mechanism!r} has no pinned burn contract")
    if not isinstance(burn_hotkey, str) or not burn_hotkey:
        raise wire.VectorError(
            "authority mode requires --provenance-burn-hotkey (the configured "
            "burn destination; never taken from Cathedral's vector)"
        )
    if burn_hotkey not in hotkey_to_uid:
        raise wire.VectorError(
            f"configured burn hotkey {burn_hotkey!r} has no current metagraph UID"
        )
    burn_uid = hotkey_to_uid[burn_hotkey]

    scores: dict[int, float] = {}
    seen: set[int] = set()
    total = 0.0
    for hotkey, weight in sorted(recomputed.items()):
        value = float(weight)
        if not math.isfinite(value) or value < 0.0:
            raise wire.VectorError(
                f"recomputed weight for {hotkey!r} is non-finite or negative"
            )
        if value == 0.0:
            continue
        if hotkey not in hotkey_to_uid:
            raise wire.VectorError(
                f"provenance hotkey {hotkey!r} has no current metagraph UID"
            )
        uid = hotkey_to_uid[hotkey]
        if uid == burn_uid:
            raise wire.VectorError(
                f"provenance hotkey {hotkey!r} resolves to the burn UID"
            )
        if uid in seen:
            raise wire.VectorError(f"provenance duplicate UID {uid}")
        seen.add(uid)
        scores[uid] = value
        total += value
    if scores and not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise wire.VectorError(
            f"recomputed shares sum to {total!r}, expected 1.0"
        )
    out = {uid: value * (1.0 - burn_fraction) for uid, value in scores.items()}
    out[burn_uid] = out.get(burn_uid, 0.0) + (
        burn_fraction if scores else 1.0
    )
    norm = math.fsum(out.values())
    return {uid: value / norm for uid, value in out.items()}


class _ShadowAuditor:
    """Single-flight background worker for the shadow provenance audit.

    tick() submits non-blocking; while an audit is in flight further
    submissions are skipped (single-flight). Results are drained and logged
    by the MAIN thread on a later tick, so a slow or broken audit can never
    delay, reorder, or fail the thin submission path.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._result = None  # (audit, state_file)

    def busy(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def submit(self, settings, *, network, netuid, payload, state, state_file) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False

            def _run() -> None:
                audit = run_audit(
                    settings,
                    network=network,
                    netuid=netuid,
                    vector_payload=payload,
                    state=state,
                )
                with self._lock:
                    self._result = (audit, state_file)

            self._thread = threading.Thread(
                target=_run, name="cathedral-shadow-audit", daemon=True
            )
            self._thread.start()
            return True

    def drain(self):
        with self._lock:
            result = self._result
            self._result = None
            return result


def _get_shadow_auditor(args) -> _ShadowAuditor:
    existing = getattr(args, "_shadow_auditor", None)
    if existing is not None:
        return existing
    auditor = _ShadowAuditor()
    try:
        args._shadow_auditor = auditor
    except AttributeError:
        pass
    return auditor


def _log_audit_events(args, audit, state_file: Path) -> None:
    """Log one completed audit and persist chain state (main thread only)."""
    events = _get_events(args)
    status_map = {"PASS": PASS, "FAIL": FAIL, "NOT_PROVEN": NOT_PROVEN}
    if audit.status == "PASS":
        events.event(
            "PROVENANCE_AUDIT_PASS",
            stage="provenance",
            status=PASS,
            duration_ms=audit.duration_ms,
            artifact=audit.manifest_digest,
            detail=(
                f"source_epoch={audit.source_epoch} release={audit.policy_release} "
                f"mechanism={audit.mechanism} verified_miners={len(audit.recomputed)} "
                f"vector_agrees={audit.agrees_with_vector}"
            ),
        )
        if audit.agrees_with_vector is False:
            events.event(
                "PROVENANCE_VECTOR_MISMATCH",
                stage="provenance",
                status=FAIL,
                detail="; ".join(audit.discrepancies)[:512],
                remediation=audit.remediation,
            )
            _lifecycle(
                "PROVENANCE mismatch",
                f"discrepancies={len(audit.discrepancies)}",
            )
        _write_state(state_file, {
            "provenance_last_source_epoch": audit.source_epoch,
            "provenance_last_report_id": audit.report_id,
        })
    else:
        events.event(
            "PROVENANCE_AUDIT_" + audit.status,
            stage="provenance",
            status=status_map.get(audit.status, FAIL),
            duration_ms=audit.duration_ms,
            artifact=audit.manifest_digest,
            detail=(audit.error or "; ".join(audit.discrepancies))[:512] or None,
            remediation=audit.remediation,
        )
        _lifecycle(
            "PROVENANCE " + audit.status.lower(),
            f"error={audit.error!r}" if audit.error else "",
        )


def _run_provenance_stage(
    args, payload: dict[str, Any], state_file: Path
) -> "tuple[str, dict[str, float] | None]":
    """Provenance stage for this tick.

    Shadow: a bounded SINGLE-FLIGHT background worker — the previous audit's
    result is drained and logged, a new audit is submitted without blocking,
    and the thin submission proceeds untouched regardless of audit speed or
    health. Authority: synchronous; the tick refuses to submit anything
    unless the audit PASSes at FULL assurance.
    """
    settings = _provenance_settings(args)
    if settings.mode == "authority":
        state = _read_state(state_file)
        audit = run_audit(
            settings,
            network=args.network,
            netuid=args.netuid,
            vector_payload=payload,
            state=state,
        )
        _log_audit_events(args, audit, state_file)
        if audit.status != "PASS":
            raise wire.VectorError(
                f"full-provenance authority audit did not PASS ({audit.status}): "
                f"{audit.error or 'see events'}"
            )
        if getattr(audit, "assurance", "receipts_only") != "full":
            raise wire.VectorError(
                "authority requires FULL assurance (raw-evidence replay); "
                "receipts-only recomputation is NOT PROVEN and never submits"
            )
        return audit.status, dict(audit.recomputed)

    auditor = _get_shadow_auditor(args)
    finished = auditor.drain()
    if finished is not None:
        _log_audit_events(args, finished[0], finished[1])
    submitted = auditor.submit(
        settings,
        network=args.network,
        netuid=args.netuid,
        payload=dict(payload),
        state=_read_state(state_file),
        state_file=state_file,
    )
    if not submitted:
        _get_events(args).event(
            "PROVENANCE_AUDIT_SKIPPED",
            stage="provenance",
            status=INFO,
            detail="previous shadow audit still in flight (single-flight)",
        )
    return "PENDING", None


# -- burn + uid mapping ---------------------------------------------------------

def apply_burn(scores_by_uid: dict[int, float], *, burn_uid: int | None,
               forced_burn_percentage: float) -> dict[int, float]:
    """burn% of total mass to burn_uid, remainder split proportionally across
    miners; normalized to sum 1.0. Empty miner set -> everything to burn_uid."""
    burn_frac = forced_burn_percentage / 100.0
    if burn_uid is not None:
        # burn_uid must never double-collect (miner share + forced burn);
        # any score that mapped onto it is dropped before allocation.
        scores_by_uid = {u: v for u, v in scores_by_uid.items() if u != burn_uid}
    total = sum(scores_by_uid.values())
    if total <= 0 or not scores_by_uid:
        if burn_uid is None:
            raise wire.VectorError("no miner mass and no burn_uid fallback")
        return {burn_uid: 1.0}
    out = {uid: (v / total) * (1.0 - burn_frac) for uid, v in scores_by_uid.items()}
    if burn_uid is not None and burn_frac > 0:
        out[burn_uid] = out.get(burn_uid, 0.0) + burn_frac
    norm = sum(out.values())
    return {uid: v / norm for uid, v in out.items()}


def accept_vector(payload: dict[str, Any], *, public_key_hex: str, key_id: str,
                  network: str, netuid: int, fence_version: int) -> None:
    """Every check between 'bytes arrived' and 'safe to apply'. Raises on any
    failure — there is deliberately no partial acceptance."""
    wire.verify_signature(payload, public_key_hex=public_key_hex, expected_key_id=key_id)
    wire.invariant_check(payload, network=network, netuid=netuid, now_iso=_ms_iso_now())
    pv = int(payload["policy_version"])
    if pv <= fence_version:
        raise wire.VectorError(
            f"rollback/replay: vector policy_version {pv} <= last accepted {fence_version}")


def _confidential_tdx_v3_rows(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    metadata = payload.get("policy_metadata") or {}
    if not isinstance(metadata, dict):
        return None
    cap = metadata.get("confidential_tdx_cap") or {}
    if not isinstance(cap, dict) or cap.get("cap_version") != "v3":
        return None

    try:
        configured_fraction = float(cap["configured_fraction"])
    except (KeyError, TypeError, ValueError) as exc:
        raise wire.VectorError(
            "confidential_tdx v3 missing configured_fraction"
        ) from exc
    if not math.isfinite(configured_fraction) or not 0.0 < configured_fraction <= 0.10:
        raise wire.VectorError(
            f"confidential_tdx v3 invalid configured_fraction {configured_fraction!r}"
        )

    rows = payload.get("weights")
    if not isinstance(rows, list):
        raise wire.VectorError("confidential_tdx v3 weights must be a list")
    hotkeys: set[str] = set()
    weight_mass = 0.0
    base_mass = 0.0
    external_mass = 0.0
    for row in rows:
        if not isinstance(row, dict):
            raise wire.VectorError("confidential_tdx v3 weight row must be an object")
        try:
            weight = float(row["weight"])
            base = float(row["base_component"])
            external = float(row["external_component"])
        except (KeyError, TypeError, ValueError) as exc:
            raise wire.VectorError(
                "confidential_tdx v3 row missing or invalid attribution component"
            ) from exc
        if not all(math.isfinite(value) and value >= 0.0
                   for value in (weight, base, external)):
            raise wire.VectorError(
                f"confidential_tdx v3 row {row.get('miner_hotkey')!r} "
                "has non-finite or negative attribution"
            )
        if not math.isclose(weight, base + external, rel_tol=0.0, abs_tol=1e-12):
            raise wire.VectorError(
                f"confidential_tdx v3 row {row.get('miner_hotkey')!r} "
                f"weight {weight!r} != base+external {base + external!r}"
            )
        hotkey = row.get("miner_hotkey")
        if not isinstance(hotkey, str) or not hotkey:
            raise wire.VectorError("confidential_tdx v3 row missing miner_hotkey")
        if hotkey in hotkeys:
            raise wire.VectorError(f"confidential_tdx v3 duplicate hotkey {hotkey!r}")
        hotkeys.add(hotkey)
        weight_mass = math.fsum((weight_mass, weight))
        base_mass = math.fsum((base_mass, base))
        external_mass = math.fsum((external_mass, external))

    component_mass = base_mass + external_mass
    if not math.isclose(weight_mass, component_mass, rel_tol=0.0, abs_tol=1e-12):
        raise wire.VectorError(
            f"confidential_tdx v3 weight mass {weight_mass!r} != "
            f"component mass {component_mass!r}"
        )
    if base_mass <= 0.0 or external_mass <= 0.0:
        raise wire.VectorError(
            "confidential_tdx v3 requires positive base and external mass"
        )
    realized_fraction = external_mass / component_mass
    if abs(realized_fraction - configured_fraction) > 1e-12:
        raise wire.VectorError(
            f"confidential_tdx v3 external fraction {realized_fraction!r} != "
            f"configured_fraction {configured_fraction!r}"
        )
    return rows


def _confidential_primary_meta(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Detect and strictly validate the v1 confidential-primary policy metadata.

    Returns the metadata dict when the signed contract is present, else None.
    Raises VectorError on a malformed/incompatible contract (never falls back).
    """
    metadata = payload.get("policy_metadata") or {}
    if not isinstance(metadata, dict):
        return None
    cp = metadata.get("confidential_primary")
    if cp is None:
        return None
    if not isinstance(cp, dict):
        raise wire.VectorError("confidential_primary metadata must be an object")
    if cp.get("contract_version") != "v1":
        raise wire.VectorError(
            "confidential_primary unsupported contract_version "
            f"{cp.get('contract_version')!r}")
    if cp.get("source") != "cathedral_confidential_tdx":
        raise wire.VectorError(
            f"confidential_primary invalid source {cp.get('source')!r}")
    try:
        base_mass = float(cp["base_mass"])
        confidential_mass = float(cp["confidential_mass"])
    except (KeyError, TypeError, ValueError) as exc:
        raise wire.VectorError(
            "confidential_primary missing base/confidential mass") from exc
    if base_mass != 0.0:
        raise wire.VectorError(
            f"confidential_primary base_mass must be 0, got {base_mass!r}")
    if confidential_mass not in (0.0, 1.0):
        raise wire.VectorError(
            "confidential_primary confidential_mass must be 0 or 1, got "
            f"{confidential_mass!r}")
    if not isinstance(cp.get("complete"), bool):
        raise wire.VectorError("confidential_primary complete flag must be a bool")
    # When the signed contract claims positive mass (mass=1), every liveness
    # field must be explicitly asserted. A degraded vector carries mass=0 and
    # these fields may be absent/false; that is the correct signed burn state.
    if confidential_mass == 1.0:
        if cp.get("mode") != "confidential_primary":
            raise wire.VectorError(
                "confidential_primary mass=1 requires mode=confidential_primary, "
                f"got {cp.get('mode')!r}")
        if cp.get("complete") is not True:
            raise wire.VectorError(
                "confidential_primary mass=1 requires complete=true")
        if cp.get("fresh") is not True:
            raise wire.VectorError(
                "confidential_primary mass=1 requires fresh=true")
        if cp.get("confirmed") is not True:
            raise wire.VectorError(
                "confidential_primary mass=1 requires confirmed=true")
    return cp


def _confidential_primary_to_uid_weights(
        payload: dict[str, Any], cp: dict[str, Any],
        hotkey_to_uid: dict[str, int]) -> dict[int, float]:
    """Map a signed confidential-primary vector to UID weights, all-or-nothing.

    Every positive signed hotkey MUST map to exactly one current metagraph UID.
    Duplicate hotkeys, duplicate UIDs, nonfinite/negative attribution, and
    metadata/sum drift all reject the whole vector. There is no partial apply
    and no fallback. The signed burn is applied ONLY after a fully successful
    mapping.
    """
    snap = payload["burn_snapshot"]
    confidential_mass = float(cp["confidential_mass"])
    rows = payload.get("weights")
    if not isinstance(rows, list):
        raise wire.VectorError("confidential_primary weights must be a list")

    hotkeys: set[str] = set()
    weight_mass = 0.0
    positive: list[tuple[str, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise wire.VectorError("confidential_primary weight row must be an object")
        if "base_component" not in row or "external_component" not in row:
            raise wire.VectorError(
                f"confidential_primary row {row.get('miner_hotkey')!r} "
                "must carry both base_component and external_component")
        try:
            weight = float(row["weight"])
            base = float(row["base_component"])
            external = float(row["external_component"])
        except (KeyError, TypeError, ValueError) as exc:
            raise wire.VectorError(
                "confidential_primary row has invalid attribution") from exc
        if not all(math.isfinite(v) and v >= 0.0 for v in (weight, base, external)):
            raise wire.VectorError(
                f"confidential_primary row {row.get('miner_hotkey')!r} "
                "has non-finite or negative attribution")
        if base != 0.0:
            raise wire.VectorError(
                f"confidential_primary row {row.get('miner_hotkey')!r} "
                "base_component must be 0")
        if not math.isclose(weight, external, rel_tol=0.0, abs_tol=1e-12):
            raise wire.VectorError(
                f"confidential_primary row {row.get('miner_hotkey')!r} "
                "weight != external_component")
        hotkey = row.get("miner_hotkey")
        if not isinstance(hotkey, str) or not hotkey:
            raise wire.VectorError("confidential_primary row missing miner_hotkey")
        if hotkey in hotkeys:
            raise wire.VectorError(f"confidential_primary duplicate hotkey {hotkey!r}")
        hotkeys.add(hotkey)
        weight_mass = math.fsum((weight_mass, weight))
        if weight > 0.0:
            positive.append((hotkey, weight))

    # Signed metadata mass must agree with the signed rows.
    if confidential_mass == 1.0:
        if not positive:
            raise wire.VectorError(
                "confidential_primary claims mass 1 but has no positive weight")
        if not math.isclose(weight_mass, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise wire.VectorError(
                f"confidential_primary weight mass {weight_mass!r} != 1.0")
    else:  # confidential_mass == 0.0
        if positive:
            raise wire.VectorError(
                "confidential_primary claims mass 0 but has positive weight")
        if weight_mass != 0.0:
            raise wire.VectorError(
                f"confidential_primary weight mass {weight_mass!r} != 0.0")

    # Every positive signed hotkey must map to exactly one current metagraph UID.
    scores: dict[int, float] = {}
    mapped_uids: set[int] = set()
    for hotkey, weight in positive:
        if hotkey not in hotkey_to_uid:
            raise wire.VectorError(
                f"confidential_primary hotkey {hotkey!r} has no current metagraph UID")
        uid = hotkey_to_uid[hotkey]
        if uid == snap.get("burn_uid"):
            raise wire.VectorError(
                f"confidential_primary hotkey {hotkey!r} resolves to burn UID"
            )
        if uid in mapped_uids:
            raise wire.VectorError(
                f"confidential_primary duplicate UID {uid} in signed vector")
        mapped_uids.add(uid)
        scores[uid] = weight

    # Signed burn applied ONLY after a fully successful mapping.
    return apply_burn(
        scores,
        burn_uid=snap.get("burn_uid"),
        forced_burn_percentage=float(snap["forced_burn_percentage"]),
    )


# Supported policy pins. When a validator opts in, ONLY the selected signed
# contract is applied; every other vector shape (legacy, v3 blend) is rejected.
REQUIRE_POLICY_CONFIDENTIAL_PRIMARY_V1 = "confidential_primary_v1"
REQUIRE_POLICY_VALIDATED_SUPPLY_V1 = "validated_supply_v1"
REQUIRE_POLICY_CHOICES = (
    REQUIRE_POLICY_CONFIDENTIAL_PRIMARY_V1,
    REQUIRE_POLICY_VALIDATED_SUPPLY_V1,
)


def _validated_supply_meta(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the launch-locked 90/10 validated-supply allocation.

    Version 1 admits only Intel TDX rows. The independently approved GPU class
    is deliberately empty, so its exact 10% allocation is routed to burn. A
    later GPU admission requires a new, explicitly reviewed contract version.
    """
    metadata = payload.get("policy_metadata") or {}
    if not isinstance(metadata, dict):
        return None
    policy = metadata.get("validated_supply")
    if policy is None:
        return None
    if not isinstance(policy, dict):
        raise wire.VectorError("validated_supply metadata must be an object")
    expected = {
        "contract_version",
        "intel_tdx_allocation",
        "verified_gpu_allocation",
        "verified_gpu_admitted",
        "burn_hotkey",
    }
    if set(policy) != expected:
        raise wire.VectorError("validated_supply metadata fields mismatch")
    if policy["contract_version"] != "v1":
        raise wire.VectorError("validated_supply unsupported contract_version")
    try:
        tdx = float(policy["intel_tdx_allocation"])
        gpu = float(policy["verified_gpu_allocation"])
    except (TypeError, ValueError) as exc:
        raise wire.VectorError("validated_supply allocations must be numeric") from exc
    if not math.isclose(tdx, 0.90, rel_tol=0.0, abs_tol=1e-12):
        raise wire.VectorError("validated_supply Intel TDX allocation must equal 0.90")
    if not math.isclose(gpu, 0.10, rel_tol=0.0, abs_tol=1e-12):
        raise wire.VectorError("validated_supply Verified GPU allocation must equal 0.10")
    if not math.isclose(tdx + gpu, 1.0, rel_tol=0.0, abs_tol=1e-12):
        raise wire.VectorError("validated_supply allocations must sum to 1")
    if policy["verified_gpu_admitted"] is not False:
        raise wire.VectorError("validated_supply v1 cannot admit Verified GPU")
    burn_hotkey = policy["burn_hotkey"]
    snap = payload.get("burn_snapshot") or {}
    if not isinstance(burn_hotkey, str) or not burn_hotkey:
        raise wire.VectorError("validated_supply burn_hotkey is missing")
    if snap.get("burn_hotkey") != burn_hotkey:
        raise wire.VectorError("validated_supply burn_hotkey does not match snapshot")
    if snap.get("burn_uid") is not None:
        raise wire.VectorError("validated_supply burn destination must not pin a UID")
    try:
        burn_percentage = float(snap["forced_burn_percentage"])
    except (KeyError, TypeError, ValueError) as exc:
        raise wire.VectorError("validated_supply burn percentage is missing") from exc
    if not math.isclose(burn_percentage, 10.0, rel_tol=0.0, abs_tol=1e-12):
        raise wire.VectorError("validated_supply unused GPU allocation must burn 10%")
    return policy


def _resolve_burn_hotkey(
    payload: dict[str, Any], hotkey_to_uid: dict[str, int]
) -> dict[str, Any]:
    """Resolve a signed burn hotkey against this tick's metagraph snapshot."""
    snap = payload.get("burn_snapshot") or {}
    burn_hotkey = snap.get("burn_hotkey")
    if burn_hotkey is None:
        return payload
    if burn_hotkey not in hotkey_to_uid:
        raise wire.VectorError(
            f"burn hotkey {burn_hotkey!r} has no current metagraph UID"
        )
    resolved_uid = hotkey_to_uid[burn_hotkey]
    signed_uid = snap.get("burn_uid")
    if signed_uid is not None and int(signed_uid) != resolved_uid:
        raise wire.VectorError("signed burn UID does not match current burn hotkey")
    resolved = dict(payload)
    resolved["burn_snapshot"] = {**snap, "burn_uid": resolved_uid}
    return resolved


def vector_to_uid_weights(payload: dict[str, Any],
                          hotkey_to_uid: dict[str, int],
                          *, require_policy: str | None = None) -> dict[int, float]:
    original_payload = payload
    validated_supply = _validated_supply_meta(original_payload)
    payload = _resolve_burn_hotkey(original_payload, hotkey_to_uid)
    snap = payload["burn_snapshot"]
    cp = _confidential_primary_meta(payload)
    # Pinned validators apply ONLY confidential_primary v1. A vector without a
    # valid v1 policy block is rejected here; a malformed block already raised
    # in _confidential_primary_meta. The legacy and v3 branches below are
    # unreachable while the pin is active.
    if require_policy == REQUIRE_POLICY_CONFIDENTIAL_PRIMARY_V1:
        if cp is None:
            raise wire.VectorError(
                "validator pinned to confidential_primary_v1 but vector carries "
                "no confidential_primary policy block")
        return _confidential_primary_to_uid_weights(payload, cp, hotkey_to_uid)
    if require_policy == REQUIRE_POLICY_VALIDATED_SUPPLY_V1:
        if validated_supply is None:
            raise wire.VectorError(
                "validator pinned to validated_supply_v1 but vector carries "
                "no validated_supply policy block"
            )
        if cp is None:
            raise wire.VectorError(
                "validated_supply_v1 requires confidential_primary evidence"
            )
        return _confidential_primary_to_uid_weights(payload, cp, hotkey_to_uid)
    if cp is not None:
        return _confidential_primary_to_uid_weights(payload, cp, hotkey_to_uid)
    v3_rows = _confidential_tdx_v3_rows(payload)
    if v3_rows is not None:
        mapped_uids: set[int] = set()
        missing = False
        for row in v3_rows:
            hotkey = row["miner_hotkey"]
            if hotkey not in hotkey_to_uid:
                missing = True
                continue
            uid = hotkey_to_uid[hotkey]
            if uid in mapped_uids:
                raise wire.VectorError(
                    f"confidential_tdx v3 duplicate UID {uid} in signed vector"
                )
            mapped_uids.add(uid)

        if missing:
            print("  confidential_tdx v3 map incomplete; falling back to signed base components")
        scores: dict[int, float] = {}
        for row in v3_rows:
            hotkey = row["miner_hotkey"]
            if hotkey not in hotkey_to_uid:
                continue
            uid = hotkey_to_uid[hotkey]
            value = row["base_component"] if missing else row["weight"]
            if value > 0.0:
                scores[uid] = value
        return apply_burn(
            scores,
            burn_uid=snap.get("burn_uid"),
            forced_burn_percentage=float(snap["forced_burn_percentage"]),
        )

    scores: dict[int, float] = {}
    skipped = 0
    for w in payload["weights"]:
        uid = hotkey_to_uid.get(w["miner_hotkey"])
        if uid is None:
            skipped += 1          # deregistered since the vector was composed
            continue
        scores[uid] = scores.get(uid, 0.0) + float(w["weight"])
    if skipped:
        print(f"  ({skipped} hotkeys not in metagraph, skipped)")
    return apply_burn(scores, burn_uid=snap.get("burn_uid"),
                      forced_burn_percentage=float(snap["forced_burn_percentage"]))


# -- chain ----------------------------------------------------------------------


@dataclass(frozen=True)
class ChainPreflight:
    wallet: Any
    subtensor: Any
    hotkey_to_uid: dict[str, int]
    validator_hotkey: str
    validator_uid: int
    block: int | None
    min_allowed_weights: int
    max_weight_limit: float

@contextlib.contextmanager
def _isolated_argv():
    """Hide sys.argv from bittensor while it builds its own config.

    bittensor parses sys.argv to build a config and defines its OWN `--config`
    flag. When this validator is launched as `cathedral-validator serve --config
    my.toml`, that `--config` leaks into bittensor, which then tries to YAML-load
    our TOML and aborts the tick with `Error loading config` (seen on some
    bittensor versions, not all). Blanking argv around bittensor construction
    keeps the two CLIs from colliding.
    """
    saved = sys.argv
    sys.argv = sys.argv[:1]
    try:
        yield
    finally:
        sys.argv = saved


def _validate_emission_vector(uid_weights: dict[int, float]) -> None:
    if not uid_weights:
        raise wire.VectorError("chain preflight requires a non-empty vector")
    if any(
        isinstance(uid, bool)
        or not isinstance(uid, int)
        or uid < 0
        or not math.isfinite(float(value))
        or float(value) < 0.0
        for uid, value in uid_weights.items()
    ):
        raise wire.VectorError("chain preflight vector is invalid")
    total = math.fsum(float(value) for value in uid_weights.values())
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise wire.VectorError(f"chain preflight vector mass {total!r} != 1.0")


def _validate_chain_constraints(
    uid_weights: dict[int, float], preflight: ChainPreflight
) -> None:
    positive = [float(value) for value in uid_weights.values() if float(value) > 0.0]
    if len(positive) < preflight.min_allowed_weights:
        raise wire.VectorError(
            "chain preflight vector has fewer positives than min_allowed_weights"
        )
    limit = preflight.max_weight_limit
    if not math.isfinite(limit) or not 0.0 < limit <= 1.0:
        raise wire.VectorError("chain preflight max_weight_limit is invalid")
    if any(value > limit + 1e-9 for value in positive):
        raise wire.VectorError("chain preflight vector exceeds max_weight_limit")
    if len(positive) * limit < 1.0 - 1e-9:
        raise wire.VectorError("chain preflight vector cannot conserve mass")


def chain_preflight(*, network: str, netuid: int, wallet_name: str,
                    wallet_hotkey: str) -> ChainPreflight:
    """Resolve the signing validator and all UIDs from one fresh metagraph."""
    with _isolated_argv():
        import bittensor as bt
        wallet = _bt_wallet(bt)(name=wallet_name, hotkey=wallet_hotkey)
        subtensor = _bt_subtensor(bt)(network=connection_target(network))
        metagraph = subtensor.metagraph(netuid)
    raw_uids = metagraph.uids.tolist() if hasattr(metagraph.uids, "tolist") else metagraph.uids
    uids = [int(value) for value in raw_uids]
    hotkeys = [str(value) for value in metagraph.hotkeys]
    permits = [bool(value) for value in metagraph.validator_permit]
    if not (len(uids) == len(hotkeys) == len(permits)):
        raise wire.VectorError("metagraph arrays are inconsistent")
    if len(set(uids)) != len(uids) or len(set(hotkeys)) != len(hotkeys):
        raise wire.VectorError("metagraph contains duplicate UID or hotkey")
    hotkey_to_uid = dict(zip(hotkeys, uids))
    validator_hotkey = str(wallet.hotkey.ss58_address)
    if validator_hotkey not in hotkey_to_uid:
        raise wire.VectorError("validator hotkey is not registered on this subnet")
    index = hotkeys.index(validator_hotkey)
    if not permits[index]:
        raise wire.VectorError("validator hotkey lacks validator permit")
    block_raw = getattr(metagraph, "block", None)
    try:
        block = int(block_raw) if block_raw is not None else None
    except (TypeError, ValueError):
        block = None
    result = ChainPreflight(
        wallet=wallet,
        subtensor=subtensor,
        hotkey_to_uid=hotkey_to_uid,
        validator_hotkey=validator_hotkey,
        validator_uid=hotkey_to_uid[validator_hotkey],
        block=block,
        min_allowed_weights=int(subtensor.min_allowed_weights(netuid=netuid)),
        max_weight_limit=float(subtensor.max_weight_limit(netuid=netuid)),
    )
    _lifecycle(
        "PREFLIGHT complete",
        f"validator_hotkey={validator_hotkey} validator_uid={result.validator_uid} "
        f"block={block if block is not None else 'unknown'} "
        f"min_allowed={result.min_allowed_weights} max_limit={result.max_weight_limit}",
    )
    return result


def set_weights_on_chain(uid_weights: dict[int, float], *, network: str, netuid: int,
                         wallet_name: str, wallet_hotkey: str, broadcast: bool,
                         preflight: ChainPreflight | None = None) -> bool:
    _validate_emission_vector(uid_weights)
    ordered = sorted(uid_weights.items())
    preview = ",".join(f"{u}={w:.4f}" for u, w in ordered[:12]) + (
        " ..." if len(ordered) > 12 else "")
    if not broadcast:
        _lifecycle("WEIGHTS dry-run", f"uids={len(ordered)} vector={preview}")
        return True
    uids = [u for u, _ in ordered]
    vals = [w for _, w in ordered]
    try:
        if preflight is None:
            preflight = chain_preflight(
                network=network,
                netuid=netuid,
                wallet_name=wallet_name,
                wallet_hotkey=wallet_hotkey,
            )
        _validate_chain_constraints(uid_weights, preflight)
        resp = preflight.subtensor.set_weights(
            wallet=preflight.wallet,
            netuid=netuid,
            uids=uids,
            weights=vals,
            wait_for_inclusion=True,
            wait_for_finalization=True,
        )
    except Exception as exc:
        _lifecycle("CHAIN failed", f"uids={len(ordered)} reason={type(exc).__name__}")
        raise
    # newer bittensor returns an ExtrinsicResponse object (truthy even on
    # failure) — judge success by the field, not truthiness.
    ok = bool(getattr(resp, "success", resp))
    response_details = []
    receipt = getattr(resp, "extrinsic_receipt", None)
    for name in ("extrinsic_hash", "block_hash", "block_number"):
        value = getattr(receipt, name, None) or getattr(resp, name, None)
        if value:
            response_details.append(f"{name}={str(value)[:96]}")
    _lifecycle(
        "CHAIN submitted" if ok else "CHAIN failed",
        " ".join([f"uids={len(ordered)}", f"success={ok}", *response_details]),
    )
    return ok


def _bt_subtensor(bt):
    """bittensor renamed `subtensor` -> `Subtensor` across major versions."""
    return getattr(bt, "subtensor", None) or bt.Subtensor


def _bt_wallet(bt):
    return getattr(bt, "wallet", None) or bt.Wallet


def metagraph_hotkey_to_uid(*, network: str, netuid: int) -> dict[str, int]:
    with _isolated_argv():
        import bittensor as bt   # import under blanked argv — bittensor parses
        mg = _bt_subtensor(bt)(network=connection_target(network)).metagraph(netuid)
    return {hk: int(uid) for uid, hk in zip(mg.uids.tolist(), mg.hotkeys)}


# -- main loop --------------------------------------------------------------------

def tick(args) -> bool:
    _lifecycle("FEED fetch", f"source={_feed_label(args.publisher_url)}")
    payload = fetch_vector(args.publisher_url)
    _lifecycle(
        "FEED fetched",
        f"id={str(payload.get('vector_id', ''))[:8]} "
        f"policy_version={payload.get('policy_version')}",
    )
    fence = load_fence(Path(args.state_file))
    try:
        accept_vector(payload, public_key_hex=args.public_key_hex, key_id=args.key_id,
                      network=args.network, netuid=args.netuid, fence_version=fence)
    except Exception as e:
        _lifecycle("VERIFY failed", f"reason={type(e).__name__}")
        _lifecycle("VECTOR rejected", f"stage=accept reason={type(e).__name__}")
        raise
    _lifecycle("SIGNATURE valid", f"key_id={payload.get('key_id')}")
    _lifecycle(
        "FRESHNESS valid",
        f"network={payload.get('network')} netuid={payload.get('netuid')} "
        f"generated_at={payload.get('generated_at')} expires_at={payload.get('expires_at')}",
    )
    _lifecycle(
        "ROLLBACK valid",
        f"policy_version={payload.get('policy_version')} prior_fence={fence}",
    )
    _lifecycle("VECTOR accepted",
               f"id={str(payload.get('vector_id', ''))[:8]} "
               f"policy_version={payload['policy_version']} "
               f"miners={len(payload['weights'])} "
               f"burn={payload['burn_snapshot']['forced_burn_percentage']}%")
    _get_events(args).event(
        "VECTOR_ACCEPTED",
        stage="verify",
        status=PASS,
        artifact=str(payload.get("vector_id", ""))[:36] or None,
        detail=(
            f"policy_version={payload['policy_version']} "
            f"miners={len(payload['weights'])} "
            f"burn={payload['burn_snapshot']['forced_burn_percentage']}% "
            f"signature+freshness+rollback ok"
        ),
    )
    # offline is authoritative: no chain read AND no broadcast, even if
    # --broadcast was also passed (the two are contradictory; offline wins).
    preflight = None
    if args.offline:
        hk2uid = {w["miner_hotkey"]: i for i, w in enumerate(payload["weights"])}
        burn_hotkey = (payload.get("burn_snapshot") or {}).get("burn_hotkey")
        if burn_hotkey is not None and burn_hotkey not in hk2uid:
            hk2uid[burn_hotkey] = len(hk2uid)
        _lifecycle("MAP offline", "synthetic uid map, no chain access")
        broadcast = False
    else:
        broadcast = args.broadcast
        if broadcast:
            preflight = chain_preflight(
                network=args.network,
                netuid=args.netuid,
                wallet_name=args.wallet_name,
                wallet_hotkey=args.wallet_hotkey,
            )
            hk2uid = preflight.hotkey_to_uid
        else:
            hk2uid = metagraph_hotkey_to_uid(network=args.network, netuid=args.netuid)
    try:
        uid_weights = vector_to_uid_weights(
            payload, hk2uid, require_policy=getattr(args, "require_policy", None))
    except Exception as e:
        _lifecycle("VECTOR rejected", f"stage=map reason={type(e).__name__}")
        _get_events(args).event(
            "VECTOR_REJECTED", stage="map", status=FAIL,
            detail=f"reason={type(e).__name__}",
            remediation="The signed vector failed UID mapping; nothing was submitted.",
        )
        raise

    # Concurrent full-provenance stage (shadow audits; authority replaces the
    # submitted vector with the independent recomputation).
    provenance_mode = getattr(args, "provenance", "shadow") or "shadow"
    submission_authority = "thin"
    if provenance_mode != "off":
        _, recomputed = _run_provenance_stage(args, payload, Path(args.state_file))
        if provenance_mode == "authority":
            submission_authority = "full_provenance"
            uid_weights = _provenance_uid_weights(
                recomputed,
                mechanism=getattr(args, "provenance_mechanism", MECHANISM_DEFAULT)
                or MECHANISM_DEFAULT,
                burn_hotkey=getattr(args, "provenance_burn_hotkey", None),
                hotkey_to_uid=hk2uid,
            )
            _lifecycle(
                "AUTHORITY provenance",
                f"submitting the independently recomputed vector "
                f"({len(recomputed)} verified miners, fixed 10% burn to the "
                f"configured destination)",
            )

    ordered = sorted(uid_weights.items())
    preview = ",".join(f"{uid}:{weight:.6f}" for uid, weight in ordered[:12])
    if len(ordered) > 12:
        preview += ",..."
    burn_hotkey = (payload.get("burn_snapshot") or {}).get("burn_hotkey")
    burn_uid = hk2uid.get(burn_hotkey) if burn_hotkey is not None else (
        payload.get("burn_snapshot") or {}
    ).get("burn_uid")
    burn_share = uid_weights.get(int(burn_uid), 0.0) if burn_uid is not None else 0.0
    _lifecycle(
        "MAP complete",
        f"uids={len(uid_weights)} burn_uid={burn_uid} burn_share={burn_share:.6f} "
        f"vector={preview}",
    )
    ok = set_weights_on_chain(uid_weights, network=args.network, netuid=args.netuid,
                              wallet_name=args.wallet_name, wallet_hotkey=args.wallet_hotkey,
                              broadcast=broadcast, preflight=preflight)
    _get_events(args).event(
        "WEIGHTS_SUBMITTED" if (ok and broadcast) else "WEIGHTS_DRY_RUN",
        stage="submit",
        status=PASS if ok else FAIL,
        detail=(
            f"authority={submission_authority} uids={len(ordered)} "
            f"burn_uid={burn_uid} burn_share={burn_share:.6f} vector={preview}"
        ),
        artifact=str(payload.get("vector_id", ""))[:36] or None,
    )
    # Advance the fence ONLY on a real broadcast — a dry-run/offline pass must
    # not consume a version (with the pv<=fence rule that would otherwise block
    # the subsequent live broadcast of the same vector).
    if ok and broadcast:
        save_fence(Path(args.state_file), int(payload["policy_version"]), payload["vector_id"])
    return ok


def run(args) -> int:
    """The validator loop, shared by `python -m scaffold.validator_thin` and the
    `cathedral-validator serve` console command. `args` is any object carrying
    the tick attributes (an argparse Namespace or a SimpleNamespace from the
    CLI's config loader)."""
    require_policy = getattr(args, "require_policy", None)
    if require_policy:
        _lifecycle("PIN active", f"policy={require_policy}")
    provenance_mode = getattr(args, "provenance", "shadow") or "shadow"
    submission_authority = (
        "full_provenance" if provenance_mode == "authority" else "thin"
    )
    _lifecycle(
        "MODE active",
        f"submission_authority={submission_authority} provenance={provenance_mode} "
        + (
            "(thin submits; full-provenance audits concurrently)"
            if provenance_mode == "shadow"
            else "(independent recomputation submits)"
            if provenance_mode == "authority"
            else "(thin only; no provenance audit)"
        ),
    )
    _get_events(args).event(
        "STARTUP",
        stage="startup",
        status=INFO,
        detail=(
            f"submission_authority={submission_authority} "
            f"provenance={provenance_mode} policy_pin={require_policy or 'none'} "
            f"network={args.network} netuid={args.netuid}"
        ),
    )
    while True:
        try:
            tick(args)
        except Exception as e:
            print(f"tick failed: {e}")
            _get_events(args).event(
                "TICK_FAILED",
                stage="result",
                status=FAIL,
                detail=str(e)[:512],
                remediation=(
                    "Nothing was submitted this tick; the chain retains the "
                    "last vector. Fix the reported gate and the next tick "
                    "recovers automatically."
                ),
            )
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(args.interval_secs)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Cathedral thin validator (v4)")
    p.add_argument("--publisher-url", default=os.environ.get(
        "CATHEDRAL_PUBLISHER_URL", "https://api.cathedral.computer"))
    p.add_argument("--public-key-hex", default=os.environ.get(
        "CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY", DEFAULT_PUBLIC_KEY_HEX),
        help="pinned Ed25519 public key (hex); defaults to Cathedral's published key")
    p.add_argument("--key-id", default=os.environ.get(
        "CATHEDRAL_WEIGHT_POLICY_KEY_ID", "cathedral-weight-policy"))
    p.add_argument("--network", default="finney")
    p.add_argument("--chain-endpoint", default=os.environ.get(CHAIN_ENDPOINT_ENV, ""),
                   help="connect to your own subtensor RPC node (ws/wss URL) instead of the "
                        "public entrypoint; the network label is kept for signing. "
                        f"Defaults to ${CHAIN_ENDPOINT_ENV}.")
    p.add_argument("--netuid", type=int, default=39)
    p.add_argument("--wallet-name", default=os.environ.get("BT_WALLET_NAME", "validator"))
    p.add_argument("--wallet-hotkey", default=os.environ.get("BT_WALLET_HOTKEY", "default"))
    p.add_argument("--state-file", default=os.environ.get(
        "CATHEDRAL_VALIDATOR_STATE", str(Path.home() / ".cathedral" / "thin_validator.json")))
    p.add_argument("--interval-secs", type=float, default=1500.0)
    p.add_argument("--once", action="store_true", help="single tick, then exit")
    p.add_argument("--offline", action="store_true",
                   help="no chain access: verify + print only (CI / smoke)")
    p.add_argument("--broadcast", action="store_true",
                   help="actually submit weights (default: dry-run)")
    p.add_argument("--require-policy", dest="require_policy",
                   default=os.environ.get("CATHEDRAL_VALIDATOR_REQUIRE_POLICY", "").strip() or None,
                   help="pin the validator to a signed policy contract. "
                        "validated_supply_v1 locks the launch 90%% Intel TDX / "
                        "10%% unadmitted GPU-to-burn allocation. Default: unpinned.")
    p.add_argument("--provenance", choices=("off", "shadow", "authority"),
                   default=os.environ.get("CATHEDRAL_VALIDATOR_PROVENANCE", "shadow"),
                   help="full-provenance mode: 'shadow' (default) audits the "
                        "published evidence concurrently while thin mode submits; "
                        "'authority' submits the independent recomputation; "
                        "'off' disables the audit.")
    p.add_argument("--evidence-url", default=os.environ.get(
        "CATHEDRAL_EVIDENCE_URL", "") or None,
        help="public evidence base URL (default: <publisher-url>/v1/evidence)")
    p.add_argument("--evidence-dir", default=None,
                   help="local evidence store directory (testing/reproduction)")
    p.add_argument("--provenance-registry-keys", default=os.environ.get(
        "CATHEDRAL_PROVENANCE_REGISTRY_KEYS") or None,
        help="trusted policy-registry key file (JSON key_id -> base64)")
    p.add_argument("--provenance-registry-keys-digest", default=os.environ.get(
        "CATHEDRAL_PROVENANCE_REGISTRY_KEYS_DIGEST") or None)
    p.add_argument("--provenance-report-keys", default=os.environ.get(
        "CATHEDRAL_PROVENANCE_REPORT_KEYS") or None,
        help="trusted score-report key file (JSON key_id -> base64)")
    p.add_argument("--provenance-report-keys-digest", default=os.environ.get(
        "CATHEDRAL_PROVENANCE_REPORT_KEYS_DIGEST") or None)
    p.add_argument("--provenance-index-keys", default=os.environ.get(
        "CATHEDRAL_PROVENANCE_INDEX_KEYS") or None,
        help="trusted evidence-index key file (JSON key_id -> base64)")
    p.add_argument("--provenance-index-keys-digest", default=os.environ.get(
        "CATHEDRAL_PROVENANCE_INDEX_KEYS_DIGEST") or None)
    p.add_argument("--provenance-verifier-digest", default=os.environ.get(
        "CATHEDRAL_PROVENANCE_VERIFIER_DIGEST") or None,
        help="pinned Intel TDX verifier implementation digest (sha256:<hex>)")
    p.add_argument("--provenance-mechanism", default=os.environ.get(
        "CATHEDRAL_PROVENANCE_MECHANISM", MECHANISM_DEFAULT),
        help="pinned versioned reward mechanism (default validated_supply_v1)")
    p.add_argument("--provenance-controlled-dir", default=os.environ.get(
        "CATHEDRAL_PROVENANCE_CONTROLLED_DIR") or None,
        help="controlled-disclosure envelope directory (enables FULL assurance)")
    p.add_argument("--provenance-verifier-binary", default=os.environ.get(
        "CATHEDRAL_PROVENANCE_VERIFIER_BINARY") or None,
        help="local pinned verifier binary for raw-evidence replay")
    p.add_argument("--provenance-source-revision", default=os.environ.get(
        "CATHEDRAL_PROVENANCE_SOURCE_REVISION") or None,
        help="independent pin of the expected manifest source revision")
    p.add_argument("--provenance-burn-hotkey", default=os.environ.get(
        "CATHEDRAL_PROVENANCE_BURN_HOTKEY") or None,
        help="authority mode's configured burn destination hotkey (the fixed "
             "10%% mechanism burn goes here; never taken from Cathedral's "
             "signed vector)")
    p.add_argument("--provenance-index-max-age-secs", type=float, default=3600.0)
    p.add_argument("--jsonl", default=os.environ.get(
        "CATHEDRAL_VALIDATOR_JSONL") or None,
        help="append the stable JSONL event stream to this file")
    return p


def main() -> int:
    p = build_parser()
    args = p.parse_args()
    if not args.public_key_hex:
        p.error("--public-key-hex (or CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY) is required — "
                "validators must pin the orchestrator's signing key")
    if args.require_policy and args.require_policy not in REQUIRE_POLICY_CHOICES:
        p.error(f"--require-policy (or CATHEDRAL_VALIDATOR_REQUIRE_POLICY) must be one of "
                f"{', '.join(REQUIRE_POLICY_CHOICES)}; got {args.require_policy!r}")
    # --chain-endpoint populates the env the resolver reads, so both the
    # validator_thin path and the ChainClient path honor it from one source.
    if args.chain_endpoint:
        os.environ[CHAIN_ENDPOINT_ENV] = args.chain_endpoint
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
