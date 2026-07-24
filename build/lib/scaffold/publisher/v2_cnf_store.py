"""V2 per-miner CNF body store — bake once, verify reads the stored CNF.

Per-miner V2 CNFs are generated deterministically from (hotkey, epoch, tier,
seq) via `per_miner.generate_instance`. That generation is cheap for the
default planted path (LRU-cached in per_miner.py) but expensive for REAL
(unplanted) instances — combinatorial coloring/latin-square generation with
internal retries (see real_corpus.py, CATHEDRAL_V2_CHALLENGE_SOURCE /
CATHEDRAL_PERMINER_REAL_FRACTION). The V2 async verify paths consult this store
before falling back to from-seed regeneration, which is the dominant cost of a
cold verify.

This module is a tiny, best-effort cache of the CNF body keyed by
challenge_id, populated once at mint time (challenge list) and at submit
time (bitset submit), and consulted by the verify worker before it falls
back to regenerating from seed. A challenge_id's CNF is immutable, so this
is a pure bake-and-reuse cache with no invalidation path other than time-based
purge of old rows.

UNBREAKABLE-FOR-THE-LIVE-PATH CONTRACT:
  * Every function here catches ALL exceptions internally. A store failure
    (DB down, corrupt row, bad zlib, whatever) NEVER propagates into a
    request handler or the verify tick — it just behaves as a cache miss
    (get() -> None) or a no-op write (put() -> False).
  * get() is sha-gated: callers pass the expected cnf_sha256 from the event
    row. A mismatch (stale/corrupt entry) is treated as a miss, never used.
    This makes the witness check's input impossible to poison via this cache
    — worst case is a wasted regeneration, never a wrong CNF.
  * Two independent env kill-switches let ops turn reads and/or writes off in
    seconds without a deploy that touches app logic:
      CATHEDRAL_V2_CNF_STORE_READ  (default "1") — get() short-circuits to
        None when falsy.
      CATHEDRAL_V2_CNF_STORE_WRITE (default "1") — put() short-circuits to
        False when falsy.

Storage: cnf_text is zlib-compressed before writing (v2_cnf_store.cnf_zlib is
BLOB/BYTEA) — CNFs are plain-text DIMACS and compress well, and this keeps a
backlog-sized cache from bloating the DB.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone

from .store import Store


def _env_flag(name: str, default: str) -> bool:
    val = os.environ.get(name, default)
    return str(val).strip().lower() not in ("0", "false", "no", "off", "")


def read_enabled() -> bool:
    return _env_flag("CATHEDRAL_V2_CNF_STORE_READ", "1")


def write_enabled() -> bool:
    return _env_flag("CATHEDRAL_V2_CNF_STORE_WRITE", "1")


def _now_iso_ms() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _iso_minus(hours: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=float(hours))
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def put(store: Store, challenge_id: str, cnf_text: str) -> bool:
    """Best-effort bake of a CNF body, keyed by challenge_id. Idempotent
    (INSERT OR IGNORE — the first writer for a challenge_id wins; CNFs are
    immutable so a duplicate write is always the same bytes anyway).

    Returns True on a (best-effort) successful write attempt, False if the
    write kill-switch is off or ANY error occurred. Never raises.
    """
    if not write_enabled():
        return False
    try:
        sha = hashlib.sha256(cnf_text.encode("utf-8")).hexdigest()
        blob = zlib.compress(cnf_text.encode("utf-8"))
        now_iso = _now_iso_ms()

        def _tx(conn):
            conn.execute(
                "INSERT OR IGNORE INTO v2_cnf_store"
                "(challenge_id, cnf_sha256, cnf_zlib, created_at_iso) "
                "VALUES (?, ?, ?, ?)",
                (challenge_id, sha, blob, now_iso),
            )

        store.write(_tx)
        return True
    except Exception:
        return False


def get(store: Store, challenge_id: str, expected_sha256: str | None = None) -> str | None:
    """Return the cached cnf_text for challenge_id, or None on a miss.

    A miss occurs when: the read kill-switch is off, there is no row, the
    row's cnf_sha256 does not match `expected_sha256` (when given — stale or
    corrupt entry, deliberately NOT trusted), or any error occurs decoding the
    stored bytes. Never raises.
    """
    if not read_enabled():
        return None
    try:
        rows = store.query(
            "SELECT cnf_sha256, cnf_zlib FROM v2_cnf_store WHERE challenge_id=?",
            (challenge_id,),
        )
        if not rows:
            return None
        row = rows[0]
        sha = str(row["cnf_sha256"])
        if expected_sha256 and sha != str(expected_sha256):
            return None
        raw = row["cnf_zlib"]
        if isinstance(raw, memoryview):
            raw = raw.tobytes()
        elif not isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw)
        cnf_text = zlib.decompress(bytes(raw)).decode("utf-8")
        # Belt-and-braces: the decompressed body must itself hash to the row's
        # recorded sha (and to expected_sha256 when given) — never serve bytes
        # that don't match their own claimed hash.
        actual_sha = hashlib.sha256(cnf_text.encode("utf-8")).hexdigest()
        if actual_sha != sha:
            return None
        if expected_sha256 and actual_sha != str(expected_sha256):
            return None
        return cnf_text
    except Exception:
        return None


def retention_hours() -> float:
    """Purge window for baked CNFs, in hours.

    Default 4h: epochs are hourly, so current epoch (<=1h old) + previous
    epoch grace + verify-backlog headroom. The old 24h default let the store
    accumulate ~8.7GB/day under all-miner load and filled the disk
    (2026-07-09 incident). Clamped to >=2h so a bad env value can never purge
    the current epoch's CNFs out from under the verifier.

    Env: CATHEDRAL_V2_CNF_STORE_RETENTION_HOURS
    """
    try:
        val = float(os.environ.get("CATHEDRAL_V2_CNF_STORE_RETENTION_HOURS", "4") or "4")
    except Exception:
        val = 4.0
    return max(2.0, val)


def purge_older_than(store: Store, hours: float = 24) -> int:
    """Delete rows older than `hours`. Best-effort; returns the number of rows
    deleted, or 0 on any error (including when disabled — there is no kill
    switch dedicated to purge; it is safe to run even with read/write off).
    """
    try:
        cutoff = _iso_minus(hours)

        def _tx(conn):
            cur = conn.execute(
                "DELETE FROM v2_cnf_store WHERE created_at_iso < ?", (cutoff,)
            )
            return int(cur.rowcount or 0)

        return int(store.write(_tx) or 0)
    except Exception:
        return 0


# ---- opportunistic purge throttle (module-level, best-effort) -------------
# The verify worker loop calls maybe_purge_older_than() once per tick; this
# guard makes that a no-op except at most once per `min_interval_secs`. This is
# runtime-only bookkeeping (a plain module-level timestamp), not workflow
# state, so it does not need to survive a restart or be shared across
# processes — worst case after a restart is one extra purge sweep.
_purge_lock = threading.Lock()
_last_purge_at = 0.0


def maybe_purge_older_than(store: Store, hours: float = 24, min_interval_secs: float = 600.0) -> int:
    global _last_purge_at
    try:
        now = time.time()
        with _purge_lock:
            if now - _last_purge_at < float(min_interval_secs):
                return 0
            _last_purge_at = now
        return purge_older_than(store, hours=hours)
    except Exception:
        return 0
