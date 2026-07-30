"""Mechanism Router — the ``scored → weights`` stage of the Verified Artifact
Engine, generalized so every proof rail (SAT now; Hermes / Secure-Compute
later) is a *mechanism*. One allocation table sets how much weight each
mechanism gets; changing the mix is one edit.

Authoritative interface: ``deploy/MECHANISM_ROUTER_CONTRACT.md``. Do not rename
functions or change signatures.

Guardrails baked in here:
  - Default OFF: with no enabled/positive-fraction mechanisms, ``compose``
    returns an empty dict and the CALLER keeps its pure V1 vector — output is
    byte-identical to today's V1-only path.
  - Deterministic: no unseeded randomness anywhere.
  - No secrets in code or logs.
  - A mechanism that cannot prove its scores this cycle forfeits its share.
    ``compose`` always reports that share as ``debug["forfeited_fraction"]``,
    and ``preserve_forfeited=True`` keeps it out of every other mechanism's
    weight so the caller can burn it. See ``compose``'s docstring.
  - A spec carrying ``requires_forfeit_preservation=True`` CANNOT be composed
    without that option: ``compose`` raises ``ForfeitPreservationRequired``
    rather than quietly renormalizing its forfeited share onto other
    mechanisms. That makes forfeiture-preserving composition the single
    mandatory entry point for any such mechanism.
"""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# uid -> nonnegative score
ScoreVector = dict[int, float]


@dataclass(frozen=True)
class MechanismSpec:
    mechanism_id: str          # unique slug
    owner_pubkey: str          # ss58; the key allowed to post this mechanism's scores
    weight_fraction: float     # 0..1 — THE ONE KNOB
    tier: str                  # "signed" (claims) | "artifact" (proof-backed)
    owner_uid: int | None = None  # UID controlled by the owner, for self-weight blocking
    enabled: bool = True
    # When True, this mechanism may only be composed with preserve_forfeited=True.
    # A mechanism whose unproven share MUST burn cannot be allowed to reach the
    # renormalizing path, where that share would silently move to whoever did
    # contribute. ``compose`` raises ForfeitPreservationRequired instead of
    # falling back. Defaults False so every pre-existing spec is unaffected.
    requires_forfeit_preservation: bool = False


class ForfeitPreservationRequired(RuntimeError):
    """A spec that requires forfeiture preservation was composed without it.

    Raised, never swallowed: activating such a mechanism through the legacy
    renormalizing path is a configuration error, not a degraded mode.
    """


@dataclass(frozen=True)
class ScoreVectorMeta:
    mechanism_id: str
    signed_at_ms: int
    sig_ok: bool
    source: str                # "signed_post" | "sat_adapter" | ...


class MechanismStore(Protocol):
    def list_specs(self) -> list[MechanismSpec]: ...
    def get_spec(self, mechanism_id: str) -> MechanismSpec | None: ...
    def upsert_spec(self, spec: MechanismSpec) -> None: ...
    def put_scores(self, mechanism_id: str, scores: ScoreVector, meta: ScoreVectorMeta) -> None: ...
    # returns latest (scores, meta) or None; caller decides staleness from meta.signed_at_ms
    def get_scores(self, mechanism_id: str) -> tuple[ScoreVector, ScoreVectorMeta] | None: ...


# ---------------------------------------------------------------------------
# compose — the deterministic scored→weights kernel
# ---------------------------------------------------------------------------

def compose(
    specs: list[MechanismSpec],
    scores: dict[str, tuple[ScoreVector, ScoreVectorMeta]],
    *,
    registered_uids: set[int],
    block_self_weight: bool = True,
    max_score_age_ms: int | None = None,
    now_ms: int,
    preserve_forfeited: bool = False,
) -> tuple[dict[int, float], dict]:
    """Return (final_uid_weights, debug_metadata).

    See ``deploy/MECHANISM_ROUTER_CONTRACT.md`` for the authoritative spec.
    Deterministic: no unseeded randomness. Empty result => caller keeps V1.

    ``debug["forfeited_fraction"]`` is ALWAYS the summed ``weight_fraction`` of
    the enabled, positive-fraction mechanisms that did not contribute this cycle
    (missing scores, bad signature, stale, or empty after filtering).

    ``preserve_forfeited`` selects what happens to that forfeited mass:

      * ``False`` (default, unchanged legacy behavior): the surviving mechanisms
        are renormalized to sum 1, which silently REALLOCATES a forfeited share
        to the mechanisms that did contribute.
      * ``True``: no renormalization. Each contributing mechanism keeps exactly
        its own ``weight_fraction``, the returned weights sum to
        ``debug["contributing_fraction"]``, and the caller is responsible for
        sending ``debug["forfeited_fraction"]`` to burn. That is the only mode in
        which a mechanism's unproven share can never be paid to another miner or
        another lane.
    """
    per_mech_debug: dict[str, dict] = {}
    combined: dict[int, float] = {}
    forfeited_fraction = 0.0
    contributing_fraction = 0.0

    if not preserve_forfeited:
        # Refuse, do not degrade: a mechanism that declares its unproven share
        # must burn cannot be activated through the renormalizing path.
        blocking = sorted(
            spec.mechanism_id
            for spec in specs
            if spec.requires_forfeit_preservation
            and spec.enabled
            and float(spec.weight_fraction) > 0.0
        )
        if blocking:
            raise ForfeitPreservationRequired(
                "mechanism(s) " + ", ".join(blocking) + " require "
                "preserve_forfeited=True; composing them with renormalization "
                "would reallocate a forfeited share instead of burning it"
            )

    # Deterministic iteration order over mechanisms.
    for spec in sorted(specs, key=lambda s: s.mechanism_id):
        fraction = float(spec.weight_fraction)

        # consider only enabled specs with weight_fraction > 0
        if not spec.enabled:
            per_mech_debug[spec.mechanism_id] = {
                "fraction": fraction,
                "contributing": False,
                "n_uids": 0,
                "fallback_reason": "disabled",
            }
            continue
        if fraction <= 0.0:
            per_mech_debug[spec.mechanism_id] = {
                "fraction": fraction,
                "contributing": False,
                "n_uids": 0,
                "fallback_reason": "zero_fraction",
            }
            continue

        entry = scores.get(spec.mechanism_id)
        fallback_reason: str | None = None

        if entry is None:
            fallback_reason = "missing"
        else:
            vec, meta = entry
            if not meta.sig_ok:
                fallback_reason = "sig_bad"
            elif max_score_age_ms is not None and (now_ms - int(meta.signed_at_ms)) > max_score_age_ms:
                fallback_reason = "stale"

        if fallback_reason is not None:
            forfeited_fraction += fraction
            per_mech_debug[spec.mechanism_id] = {
                "fraction": fraction,
                "contributing": False,
                "n_uids": 0,
                "fallback_reason": fallback_reason,
            }
            continue

        vec, meta = entry  # type: ignore[misc]

        # drop uids not in registered_uids; treat negatives as 0 (scores are
        # defined nonnegative, clamp defensively for a well-defined normalize).
        filtered: dict[int, float] = {}
        for uid, raw in vec.items():
            uid_i = int(uid)
            if uid_i not in registered_uids:
                continue
            val = float(raw)
            if val <= 0.0:
                continue
            filtered[uid_i] = val

        # block_self_weight: force this mechanism's score on its OWN owner_uid to 0
        if block_self_weight and spec.owner_uid is not None:
            filtered.pop(int(spec.owner_uid), None)

        total = sum(filtered.values())
        if total <= 0.0:
            forfeited_fraction += fraction
            per_mech_debug[spec.mechanism_id] = {
                "fraction": fraction,
                "contributing": False,
                "n_uids": 0,
                "fallback_reason": "empty_after_filter",
            }
            continue

        # normalize each surviving mechanism vector to sum 1, then
        # combined[uid] += fraction * normalized[uid]
        for uid_i, val in filtered.items():
            combined[uid_i] = combined.get(uid_i, 0.0) + fraction * (val / total)

        contributing_fraction += fraction
        per_mech_debug[spec.mechanism_id] = {
            "fraction": fraction,
            "contributing": True,
            "n_uids": len(filtered),
            "fallback_reason": None,
        }

    combined_total = sum(combined.values())
    debug: dict = {
        "mechanisms": per_mech_debug,
        "combined_total_before_renorm": combined_total,
        "block_self_weight": block_self_weight,
        "max_score_age_ms": max_score_age_ms,
        "now_ms": now_ms,
        "forfeited_fraction": forfeited_fraction,
        "contributing_fraction": contributing_fraction,
        "preserve_forfeited": bool(preserve_forfeited),
    }

    # No contributing mass at all → empty (caller keeps V1 / burns the lot).
    if combined_total <= 0.0:
        debug["n_final_uids"] = 0
        return {}, debug

    if preserve_forfeited:
        # Each contributing mechanism keeps exactly its own weight_fraction, so
        # a forfeited share stays forfeited. The caller sends
        # debug["forfeited_fraction"] to burn.
        debug["n_final_uids"] = len(combined)
        return dict(combined), debug

    final = {uid: w / combined_total for uid, w in combined.items()}
    debug["n_final_uids"] = len(final)
    return final, debug


# ---------------------------------------------------------------------------
# SQLite-backed MechanismStore
# ---------------------------------------------------------------------------

_DEFAULT_DB_PATH = "./data/mechanisms.sqlite3"


def default_db_path() -> str:
    return os.environ.get("CATHEDRAL_MECH_DB_PATH", _DEFAULT_DB_PATH).strip() or _DEFAULT_DB_PATH


class SqliteMechanismStore:
    """Small, self-contained SQLite MechanismStore.

    One row per mechanism spec and (latest-only) one row per mechanism scores.
    A connection is opened per operation so the store is safe to share across
    threads (FastAPI worker threads). ``:memory:`` keeps one persistent
    connection so the in-memory DB survives across per-call opens in-process.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._path = (db_path if db_path is not None else default_db_path())
        self._lock = threading.Lock()
        self._memory_conn: sqlite3.Connection | None = None
        if self._path != ":memory:":
            parent = Path(self._path).expanduser().parent
            if str(parent):
                parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # -- connection management -------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        if self._path == ":memory:":
            # Keep one persistent connection for in-memory DBs (used by tests).
            if self._memory_conn is None:
                self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
            return self._memory_conn
        return sqlite3.connect(self._path, check_same_thread=False)

    def _close(self, conn: sqlite3.Connection) -> None:
        if self._path != ":memory:":
            conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mechanism_specs (
                        mechanism_id    TEXT PRIMARY KEY,
                        owner_pubkey    TEXT NOT NULL,
                        weight_fraction REAL NOT NULL,
                        tier            TEXT NOT NULL,
                        owner_uid       INTEGER,
                        enabled         INTEGER NOT NULL DEFAULT 1,
                        requires_forfeit_preservation INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                # Idempotent migration: a spec's forfeit-preservation flag must
                # survive the store, or compose_and_publish reads it back False and
                # renormalizes a forfeited share onto contributing miners. Older DBs
                # created before this column get it here with the safe default.
                have = {r[1] for r in conn.execute("PRAGMA table_info(mechanism_specs)")}
                if "requires_forfeit_preservation" not in have:
                    conn.execute(
                        "ALTER TABLE mechanism_specs ADD COLUMN "
                        "requires_forfeit_preservation INTEGER NOT NULL DEFAULT 0"
                    )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mechanism_scores (
                        mechanism_id TEXT PRIMARY KEY,
                        scores_json  TEXT NOT NULL,
                        signed_at_ms INTEGER NOT NULL,
                        sig_ok       INTEGER NOT NULL,
                        source       TEXT NOT NULL
                    )
                    """
                )
                conn.commit()
            finally:
                self._close(conn)

    # -- MechanismStore protocol ----------------------------------------------
    def list_specs(self) -> list[MechanismSpec]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT mechanism_id, owner_pubkey, weight_fraction, tier, owner_uid, enabled, "
                    "requires_forfeit_preservation "
                    "FROM mechanism_specs ORDER BY mechanism_id"
                ).fetchall()
            finally:
                self._close(conn)
        return [self._row_to_spec(r) for r in rows]

    def get_spec(self, mechanism_id: str) -> MechanismSpec | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT mechanism_id, owner_pubkey, weight_fraction, tier, owner_uid, enabled, "
                    "requires_forfeit_preservation "
                    "FROM mechanism_specs WHERE mechanism_id = ?",
                    (mechanism_id,),
                ).fetchone()
            finally:
                self._close(conn)
        return self._row_to_spec(row) if row is not None else None

    def upsert_spec(self, spec: MechanismSpec) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO mechanism_specs
                        (mechanism_id, owner_pubkey, weight_fraction, tier, owner_uid, enabled,
                         requires_forfeit_preservation)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(mechanism_id) DO UPDATE SET
                        owner_pubkey=excluded.owner_pubkey,
                        weight_fraction=excluded.weight_fraction,
                        tier=excluded.tier,
                        owner_uid=excluded.owner_uid,
                        enabled=excluded.enabled,
                        requires_forfeit_preservation=excluded.requires_forfeit_preservation
                    """,
                    (
                        spec.mechanism_id,
                        spec.owner_pubkey,
                        float(spec.weight_fraction),
                        spec.tier,
                        None if spec.owner_uid is None else int(spec.owner_uid),
                        1 if spec.enabled else 0,
                        1 if spec.requires_forfeit_preservation else 0,
                    ),
                )
                conn.commit()
            finally:
                self._close(conn)

    def put_scores(self, mechanism_id: str, scores: ScoreVector, meta: ScoreVectorMeta) -> None:
        # JSON keys are strings; store canonically and restore ints on read.
        scores_json = json.dumps({str(int(k)): float(v) for k, v in scores.items()}, sort_keys=True)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO mechanism_scores
                        (mechanism_id, scores_json, signed_at_ms, sig_ok, source)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(mechanism_id) DO UPDATE SET
                        scores_json=excluded.scores_json,
                        signed_at_ms=excluded.signed_at_ms,
                        sig_ok=excluded.sig_ok,
                        source=excluded.source
                    """,
                    (
                        mechanism_id,
                        scores_json,
                        int(meta.signed_at_ms),
                        1 if meta.sig_ok else 0,
                        meta.source,
                    ),
                )
                conn.commit()
            finally:
                self._close(conn)

    def get_scores(self, mechanism_id: str) -> tuple[ScoreVector, ScoreVectorMeta] | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT mechanism_id, scores_json, signed_at_ms, sig_ok, source "
                    "FROM mechanism_scores WHERE mechanism_id = ?",
                    (mechanism_id,),
                ).fetchone()
            finally:
                self._close(conn)
        if row is None:
            return None
        raw = json.loads(row[1])
        vec: ScoreVector = {int(k): float(v) for k, v in raw.items()}
        meta = ScoreVectorMeta(
            mechanism_id=row[0],
            signed_at_ms=int(row[2]),
            sig_ok=bool(row[3]),
            source=row[4],
        )
        return vec, meta

    @staticmethod
    def _row_to_spec(row) -> MechanismSpec:
        return MechanismSpec(
            mechanism_id=row[0],
            owner_pubkey=row[1],
            weight_fraction=float(row[2]),
            tier=row[3],
            owner_uid=None if row[4] is None else int(row[4]),
            enabled=bool(row[5]),
            requires_forfeit_preservation=bool(row[6]),
        )


# ---------------------------------------------------------------------------
# Admin-gated PUT /mechanisms/{id} — exposed as an APIRouter (NOT auto-wired).
# The integrator mounts this on the app; it reuses the publisher admin-token
# pattern from app.py (env CATHEDRAL_PUBLISHER_ADMIN_TOKEN, bearer, constant-time
# compare). Default OFF: unset token => 503.
# ---------------------------------------------------------------------------

def _bearer_value(authorization: str | None) -> str:
    supplied = (authorization or "").strip()
    if supplied.lower().startswith("bearer "):
        supplied = supplied.split(" ", 1)[1].strip()
    return supplied


def _publisher_admin_token_configured() -> str:
    return os.environ.get("CATHEDRAL_PUBLISHER_ADMIN_TOKEN", "").strip()


# Request body for PUT /mechanisms/{id}. Defined at module level (pydantic only,
# not FastAPI) so FastAPI can resolve the forward-ref annotation on the endpoint
# under ``from __future__ import annotations``.
from pydantic import BaseModel, Field  # noqa: E402


class MechanismSpecBody(BaseModel):
    owner_pubkey: str
    weight_fraction: float = Field(ge=0.0, le=1.0)
    tier: str
    owner_uid: int | None = None
    enabled: bool = True
    # A lane that must forfeit an uncredited share to burn rather than have it
    # renormalized onto contributing miners declares it here, so the flag is
    # persisted with the spec and enforced by every compose path.
    requires_forfeit_preservation: bool = False


_STORE_SINGLETON: SqliteMechanismStore | None = None


def get_default_store() -> SqliteMechanismStore:
    """Process-wide default store bound to CATHEDRAL_MECH_DB_PATH."""
    global _STORE_SINGLETON
    if _STORE_SINGLETON is None:
        _STORE_SINGLETON = SqliteMechanismStore()
    return _STORE_SINGLETON


def create_router(store: MechanismStore | None = None):
    """Build the admin-gated mechanisms APIRouter.

    Kept import-light so importing this module for ``compose`` never requires
    FastAPI. Pass a ``store`` to inject one (tests); defaults to the process
    SQLite store. The request body model is defined at module level so FastAPI
    can resolve it under ``from __future__ import annotations``.
    """
    from fastapi import APIRouter, Header, HTTPException

    router = APIRouter()

    def _resolve_store() -> MechanismStore:
        return store if store is not None else get_default_store()

    def _require_publisher_admin(authorization: str | None) -> None:
        token = _publisher_admin_token_configured()
        if not token:
            raise HTTPException(503, "publisher_admin_token_not_configured")
        if not hmac.compare_digest(_bearer_value(authorization), token):
            raise HTTPException(401, "invalid_admin_token")

    @router.put("/mechanisms/{mechanism_id}")
    def upsert_mechanism(
        mechanism_id: str,
        body: MechanismSpecBody,
        authorization: str | None = Header(None),
    ):
        _require_publisher_admin(authorization)
        spec = MechanismSpec(
            mechanism_id=mechanism_id,
            owner_pubkey=body.owner_pubkey,
            weight_fraction=float(body.weight_fraction),
            tier=body.tier,
            owner_uid=body.owner_uid,
            enabled=body.enabled,
            requires_forfeit_preservation=body.requires_forfeit_preservation,
        )
        _resolve_store().upsert_spec(spec)
        return {
            "ok": True,
            "mechanism": {
                "mechanism_id": spec.mechanism_id,
                "owner_pubkey": spec.owner_pubkey,
                "weight_fraction": spec.weight_fraction,
                "tier": spec.tier,
                "owner_uid": spec.owner_uid,
                "enabled": spec.enabled,
                "requires_forfeit_preservation": spec.requires_forfeit_preservation,
            },
        }

    @router.post("/mechanisms/refresh")
    def refresh_mechanisms(
        authorization: str | None = Header(None),
        epoch: int | None = None,
    ):
        """Run one artifact-tier refresh cycle: compute each enabled artifact
        mechanism's scores from its adapter and persist them for ``compose``. The
        operator's scheduler owns the cadence by how often it calls this. Read-only
        except ``put_scores`` of each mechanism's own row; never composes or writes
        weights. Admin-token gated, exactly like the upsert above."""
        _require_publisher_admin(authorization)
        # Lazy import: mechanism_artifact_refresh imports this module, so importing it
        # at module load would cycle (and this module stays FastAPI-free at import).
        from . import mechanism_artifact_refresh as _refresh
        refreshed = _refresh.refresh_artifact_scores(_resolve_store(), epoch=epoch)
        return {"ok": True, "refreshed": refreshed}

    return router


def __getattr__(name: str):
    # Lazily build a module-level ``router`` (default store) only when accessed,
    # so ``import mechanism_router`` for compose() never imports FastAPI.
    if name == "router":
        return create_router()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
