"""Immutable, content-addressed V2 per-miner CNF publication.

This module publishes CNF *bytes only*.  Authentication, ownership checks, and
submit-token minting stay in the live app and are deliberately absent here.
Objects use a versioned SHA-256 key and are read back after publication before
their catalog row can be recorded:

    v2/cnf/v1/sha256/<sha256>.cnf

The catalog contains only non-secret delivery metadata.  A separate readiness
row is marked ready only after every configured artifact for both the requested
epoch and its successor has been verified.  Re-running a publish is idempotent;
an existing object or catalog row with different bytes/identity fails closed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from . import per_miner as pm
from . import v2_cnf_store
from .store import Store

ARTIFACT_VERSION = "v1"
ARTIFACT_SCHEMA = "cathedral.v2.cnf_artifact.v1"
ACCESS_SCHEMA = "cathedral.v2.cnf_access.v1"
ARTIFACT_CACHE_CONTROL = "public, max-age=31536000, immutable"
ARTIFACT_CONTENT_TYPE = "text/plain; charset=utf-8"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

PutObject = Callable[[str, bytes, str, str], Any]
GetObject = Callable[[str], bytes | None]
Progress = Callable[[str, dict[str, Any]], None]


class ArtifactPublicationError(RuntimeError):
    """Base class for fail-closed artifact publication errors."""


class ArtifactMismatchError(ArtifactPublicationError):
    """An immutable object/catalog entry disagrees with its claimed identity."""


@dataclass(frozen=True)
class ArtifactIdentity:
    sha256: str
    length: int
    key: str


def artifact_key(cnf_sha256: str) -> str:
    sha = str(cnf_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(sha):
        raise ValueError("invalid_cnf_sha256")
    return f"v2/cnf/{ARTIFACT_VERSION}/sha256/{sha}.cnf"


def content_addressed_identity(cnf_bytes: bytes) -> ArtifactIdentity:
    if not isinstance(cnf_bytes, (bytes, bytearray)):
        raise TypeError("cnf artifact body must be bytes")
    body = bytes(cnf_bytes)
    sha = hashlib.sha256(body).hexdigest()
    return ArtifactIdentity(sha256=sha, length=len(body), key=artifact_key(sha))


def immutable_artifact_url(base_url: str, key: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("cnf_artifact_base_url_missing")
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid_cnf_artifact_base_url")
    expected_prefix = f"v2/cnf/{ARTIFACT_VERSION}/sha256/"
    if not str(key).startswith(expected_prefix):
        raise ValueError("invalid_cnf_artifact_key")
    return f"{base}/{key}"


def verify_artifact_bytes(body: bytes, identity: ArtifactIdentity) -> None:
    if not isinstance(body, (bytes, bytearray)):
        raise ArtifactMismatchError("cnf_artifact_readback_not_bytes")
    actual = bytes(body)
    if len(actual) != identity.length:
        raise ArtifactMismatchError("cnf_artifact_length_mismatch")
    if hashlib.sha256(actual).hexdigest() != identity.sha256:
        raise ArtifactMismatchError("cnf_artifact_sha256_mismatch")


def _row_dict(row: Any) -> dict[str, Any]:
    try:
        return {key: row[key] for key in row.keys()}
    except Exception:
        return dict(row)


def get_artifact(store: Store, challenge_id: str) -> dict[str, Any] | None:
    """Return a validated non-secret catalog row for ``challenge_id``."""
    rows = store.query(
        "SELECT challenge_id, epoch, tier, seq, n_vars, cnf_sha256, cnf_bytes, "
        "artifact_key, artifact_url, published_at_iso "
        "FROM v2_cnf_artifacts WHERE challenge_id=?",
        (str(challenge_id),),
    )
    if not rows:
        return None
    row = _row_dict(rows[0])
    sha = str(row.get("cnf_sha256") or "").lower()
    key = str(row.get("artifact_key") or "")
    url = str(row.get("artifact_url") or "")
    try:
        expected_key = artifact_key(sha)
        length = int(row.get("cnf_bytes") or 0)
        n_vars = int(row.get("n_vars") or 0)
    except (TypeError, ValueError):
        return None
    parsed_url = urlsplit(url)
    if (
        key != expected_key
        or length <= 0
        or n_vars <= 0
        or parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
        or not parsed_url.path.endswith(f"/{key}")
    ):
        return None
    row["cnf_sha256"] = sha
    row["cnf_bytes"] = length
    row["n_vars"] = n_vars
    row["epoch"] = int(row["epoch"])
    row["tier"] = int(row["tier"])
    row["seq"] = int(row["seq"])
    return row


def _register_artifact(
    store: Store,
    *,
    challenge_id: str,
    epoch: int,
    tier: int,
    seq: int,
    n_vars: int,
    identity: ArtifactIdentity,
    artifact_url: str,
    published_at: str,
) -> dict[str, Any]:
    def _insert(conn):
        conn.execute(
            "INSERT OR IGNORE INTO v2_cnf_artifacts("
            "challenge_id, epoch, tier, seq, n_vars, cnf_sha256, cnf_bytes, "
            "artifact_key, artifact_url, published_at_iso"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                challenge_id,
                int(epoch),
                int(tier),
                int(seq),
                int(n_vars),
                identity.sha256,
                identity.length,
                identity.key,
                artifact_url,
                published_at,
            ),
        )

    store.write(_insert)
    row = get_artifact(store, challenge_id)
    if row is None:
        raise ArtifactMismatchError("cnf_artifact_catalog_missing_after_insert")
    expected = {
        "epoch": int(epoch),
        "tier": int(tier),
        "seq": int(seq),
        "n_vars": int(n_vars),
        "cnf_sha256": identity.sha256,
        "cnf_bytes": identity.length,
        "artifact_key": identity.key,
        "artifact_url": artifact_url,
    }
    if any(row.get(key) != value for key, value in expected.items()):
        raise ArtifactMismatchError("cnf_artifact_catalog_mismatch")
    return row


def _write_readiness(
    store: Store,
    *,
    epoch: int,
    expected_current: int,
    published_current: int,
    expected_next: int,
    published_next: int,
    ready: bool,
    updated_at: str,
) -> None:
    def _write(conn):
        conn.execute(
            "INSERT OR REPLACE INTO v2_cnf_epoch_readiness("
            "epoch, next_epoch, expected_current, published_current, "
            "expected_next, published_next, ready, updated_at_iso"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(epoch),
                int(epoch) + 1,
                int(expected_current),
                int(published_current),
                int(expected_next),
                int(published_next),
                1 if ready else 0,
                str(updated_at),
            ),
        )

    store.write(_write)


def epoch_readiness(store: Store, epoch: int) -> dict[str, Any] | None:
    rows = store.query(
        "SELECT epoch, next_epoch, expected_current, published_current, "
        "expected_next, published_next, ready, updated_at_iso "
        "FROM v2_cnf_epoch_readiness WHERE epoch=?",
        (int(epoch),),
    )
    return _row_dict(rows[0]) if rows else None


def epoch_is_ready(store: Store, epoch: int) -> bool:
    row = epoch_readiness(store, epoch)
    if row is None:
        return False
    try:
        expected_current = int(row["expected_current"])
        expected_next = int(row["expected_next"])
        return (
            int(row["ready"]) == 1
            and int(row["next_epoch"]) == int(epoch) + 1
            and expected_current > 0
            and expected_next > 0
            and int(row["published_current"]) == expected_current
            and int(row["published_next"]) == expected_next
        )
    except (KeyError, TypeError, ValueError):
        return False


def _read_existing(get_object: GetObject, key: str) -> bytes | None:
    try:
        return get_object(key)
    except FileNotFoundError:
        return None


def _publish_verified_object(
    *,
    identity: ArtifactIdentity,
    body: bytes,
    put_object: PutObject,
    get_object: GetObject,
) -> None:
    existing = _read_existing(get_object, identity.key)
    if existing is not None:
        verify_artifact_bytes(existing, identity)
        return
    put_object(
        identity.key,
        body,
        ARTIFACT_CONTENT_TYPE,
        ARTIFACT_CACHE_CONTROL,
    )
    readback = _read_existing(get_object, identity.key)
    if readback is None:
        raise ArtifactPublicationError("cnf_artifact_readback_missing")
    verify_artifact_bytes(readback, identity)


def prepublish_epoch_pair(
    store: Store,
    *,
    epoch: int,
    assignment_identities: Iterable[str],
    allotment_by_tier: dict[int, int],
    cnf_base_url: str,
    put_object: PutObject,
    get_object: GetObject,
    published_at: str,
    on_progress: Progress | None = None,
) -> dict[str, Any]:
    """Publish and verify every CNF for ``epoch`` and ``epoch + 1``.

    ``assignment_identities`` are the coldkey-collapse-aware identities used by
    ``per_miner.generate_instance``.  No raw miner hotkey, signature, auth
    header, submit token, or planted witness is written to object storage or to
    the artifact catalog.

    The readiness row starts and remains false until both epochs are complete.
    Any partial failure records exact progress and re-raises; a later identical
    run safely resumes by verifying existing content-addressed objects.
    """
    identities = sorted(
        {str(value).strip() for value in assignment_identities if str(value).strip()}
    )
    if not identities:
        raise ValueError("assignment_identities_required")
    tiers: list[tuple[int, int]] = []
    for tier, count in sorted(allotment_by_tier.items()):
        tier_i = int(tier)
        count_i = int(count)
        if tier_i not in pm.TIERS or count_i <= 0:
            raise ValueError("invalid_artifact_allotment")
        if count_i != pm.allotment_for(tier_i):
            # A readiness marker is meaningful only for the full supply the
            # authenticated handler can resolve.  Dogfood subsets may publish
            # objects, but they must never use this ready-making operation.
            raise ValueError("artifact_allotment_must_cover_full_epoch_supply")
        tiers.append((tier_i, count_i))
    if not tiers:
        raise ValueError("artifact_allotment_required")
    if {tier for tier, _count in tiers} != set(pm.TIERS):
        raise ValueError("artifact_allotment_must_cover_all_tiers")

    expected_per_epoch = len(identities) * sum(count for _tier, count in tiers)
    counts = {int(epoch): 0, int(epoch) + 1: 0}
    verified_objects: set[str] = set()
    _write_readiness(
        store,
        epoch=epoch,
        expected_current=expected_per_epoch,
        published_current=0,
        expected_next=expected_per_epoch,
        published_next=0,
        ready=False,
        updated_at=published_at,
    )

    try:
        for artifact_epoch in (int(epoch), int(epoch) + 1):
            for assignment_identity in identities:
                for tier, count in tiers:
                    for seq in range(count):
                        challenge_id, claimed_sha, n_vars, _is_real, cnf_text = (
                            pm.item_meta(assignment_identity, artifact_epoch, tier, seq)
                        )
                        body = cnf_text.encode("utf-8")
                        identity = content_addressed_identity(body)
                        if claimed_sha != identity.sha256:
                            raise ArtifactMismatchError("generated_cnf_sha256_mismatch")
                        if identity.sha256 not in verified_objects:
                            _publish_verified_object(
                                identity=identity,
                                body=body,
                                put_object=put_object,
                                get_object=get_object,
                            )
                            verified_objects.add(identity.sha256)
                        url = immutable_artifact_url(cnf_base_url, identity.key)
                        _register_artifact(
                            store,
                            challenge_id=challenge_id,
                            epoch=artifact_epoch,
                            tier=tier,
                            seq=seq,
                            n_vars=n_vars,
                            identity=identity,
                            artifact_url=url,
                            published_at=published_at,
                        )
                        # Keep the legacy body-plus-token endpoint and verifier on
                        # the exact already-verified bytes.  This is best-effort;
                        # their existing deterministic fallback remains intact.
                        v2_cnf_store.put(store, challenge_id, cnf_text)
                        counts[artifact_epoch] += 1
                        if on_progress:
                            on_progress(
                                "artifact_verified",
                                {
                                    "epoch": artifact_epoch,
                                    "challenge_id": challenge_id,
                                    "tier": tier,
                                    "seq": seq,
                                    "cnf_sha256": identity.sha256,
                                    "cnf_bytes": identity.length,
                                    "artifact_key": identity.key,
                                },
                            )
    except Exception:
        _write_readiness(
            store,
            epoch=epoch,
            expected_current=expected_per_epoch,
            published_current=counts[int(epoch)],
            expected_next=expected_per_epoch,
            published_next=counts[int(epoch) + 1],
            ready=False,
            updated_at=published_at,
        )
        raise

    _write_readiness(
        store,
        epoch=epoch,
        expected_current=expected_per_epoch,
        published_current=counts[int(epoch)],
        expected_next=expected_per_epoch,
        published_next=counts[int(epoch) + 1],
        ready=True,
        updated_at=published_at,
    )
    return {
        "epoch": int(epoch),
        "next_epoch": int(epoch) + 1,
        "assignment_identities": len(identities),
        "expected_current": expected_per_epoch,
        "published_current": counts[int(epoch)],
        "expected_next": expected_per_epoch,
        "published_next": counts[int(epoch) + 1],
        "unique_artifacts": len(verified_objects),
        "ready": epoch_is_ready(store, epoch),
    }
