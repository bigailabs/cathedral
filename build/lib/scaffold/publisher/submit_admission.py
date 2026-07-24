"""Durable submit admission + async verification (RELIABILITY_UPGRADE_PLAN Phase 4/5).

This module is the *decoupling* layer: the submit handler does only cheap work
(parse, verify signature, hash, persist a pending row, return a 202 receipt) and a
separate worker claims pending rows and runs the real SAT verification later,
ordered by server `received_at` for fairness.

It is built to **extend** the existing `per_miner_attempts` ledger and the receipts
surface rather than stand up a parallel system — every column it touches was added
additively in migration 0030, and the legacy inline submit path is untouched.

Backend notes
-------------
* Postgres: the worker claim uses ``FOR UPDATE SKIP LOCKED`` so multiple workers
  never grab the same row; ordering is ``received_at_iso, id``.
* SQLite: ``store.write`` already serializes every write under one lock + BEGIN
  IMMEDIATE, so the claim is naturally exclusive without ``SKIP LOCKED`` (which
  SQLite does not support). One worker is the norm offline/in tests.

Idempotency
-----------
``idempotency_key = sha256(miner_hotkey + challenge_id + dimacs_solution_sha256)``
is UNIQUE. A replay of the same solution returns the *existing* receipt/result
instead of creating a second attempt or paying twice.

Safety
------
* No double payout: acceptance still runs through the same atomic claim
  (``submit_signatures`` replay burn + ``lane_challenge_solves`` distinct-solver
  claim) inside one ``store.write`` transaction. The async path only moves *when*
  that transaction runs, not *what* it guarantees.
* Crash safety: a row stuck in ``verifying`` past ``locked_until_iso`` is
  reclaimable by another worker; the terminal accept transaction is idempotent on
  the signature/solve uniqueness, so a reclaim cannot double-pay.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .auth import sha256_hex

# Attempt lifecycle states (the `status` column).
STATUS_PENDING = "pending"
STATUS_VERIFYING = "verifying"
STATUS_RANKED = "ranked"          # accepted (matches legacy vocabulary)
STATUS_REJECTED = "rejected"      # terminal reject (matches legacy vocabulary)
STATUS_FAILED_RETRYABLE = "failed_retryable"

# A pending/verifying row whose idempotency replay should return "still working".
_OPEN_STATES = (STATUS_PENDING, STATUS_VERIFYING, STATUS_FAILED_RETRYABLE)
_TERMINAL_STATES = (STATUS_RANKED, STATUS_REJECTED)

RECEIPT_SCHEMA = "cathedral.submit_receipt.v2"

# challenge_kind values. "public" / "per_miner" route a claimed pending row to the
# matching authoritative finalizer. "per_miner_shadow" rows are written by the
# pm-* SHADOW path: the worker verifies them and records what it WOULD have done in
# the shadow_* columns, but NEVER touches the live solve/payout ledger — the inline
# synchronous result stays authoritative until go-live proves parity.
KIND_PUBLIC = "public"
KIND_PER_MINER = "per_miner"
KIND_PER_MINER_SHADOW = "per_miner_shadow"


def idempotency_key(
    miner_hotkey: str, challenge_id: str, dimacs_solution_sha256: str
) -> str:
    """Stable key for a (miner, challenge, solution) triple. Same solution from
    the same miner -> same key -> same receipt (no second attempt, no double pay)."""
    return sha256_hex(f"{miner_hotkey}\x00{challenge_id}\x00{dimacs_solution_sha256}")


def shadow_idempotency_key(
    miner_hotkey: str, challenge_id: str, dimacs_solution_sha256: str
) -> str:
    """Idempotency key for the pm-* SHADOW twin, NAMESPACED away from the live key.

    The shadow row lives in the SAME per_miner_attempts table under a UNIQUE
    idempotency_key index. If it reused the live ``idempotency_key`` then, after
    shadow mode is turned off and live pm-async turned on, a miner retrying the
    SAME payload would have ``admit_pending`` match the old shadow row and replay
    its receipt instead of creating the LIVE authoritative pm receipt — the miner
    would never get a real ranked receipt (and could be silently un-paid). The
    ``shadow:`` prefix in the hashed material guarantees the shadow twin can never
    collide with — or be matched by — the live admission lookup."""
    return sha256_hex(
        f"shadow\x00{miner_hotkey}\x00{challenge_id}\x00{dimacs_solution_sha256}")


def challenge_kind(challenge_id: str | None) -> str:
    if not challenge_id:
        return "unknown"
    return "per_miner" if challenge_id.startswith("pm-") else "public"


# ---------------------------------------------------------------------------
# Receipt shape (the 202 body + GET /v1/agents/receipts/{id} body).
# ---------------------------------------------------------------------------
def receipt_from_row(row: Any) -> dict[str, Any]:
    """Build the public receipt JSON from a per_miner_attempts row (sqlite3.Row or
    DictRow). Only durable, non-sensitive fields are exposed — never the raw body."""
    def g(key: str, default: Any = None) -> Any:
        try:
            v = row[key]
        except Exception:
            return default
        return v if v is not None else default

    status = str(g("status", STATUS_PENDING))
    receipt_id = str(g("id"))
    challenge_id = str(g("challenge_id"))
    out: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "status": status,
        "open": status in _OPEN_STATES,
        "terminal": status in _TERMINAL_STATES,
        "receipt_id": receipt_id,
        "challenge_id": challenge_id,
        "miner_hotkey": str(g("miner_hotkey")),
        "received_at": g("received_at_iso") or g("submitted_at"),
        "dimacs_solution_sha256": str(g("dimacs_solution_sha256")),
        "receipt_url": f"/v1/agents/receipts/{receipt_id}",
    }
    verified_at = g("verified_at_iso")
    if verified_at is not None:
        out["verified_at"] = verified_at
    if status == STATUS_RANKED:
        out["weighted_score"] = g("weighted_score")
        out["solve_rank"] = g("solve_rank")
        out["eval_run_id"] = g("eval_run_id")
    elif status == STATUS_REJECTED:
        out["rejection_reason"] = g("rejection_reason")
    return out


# ---------------------------------------------------------------------------
# Admission: persist a pending receipt (cheap, idempotent).
# ---------------------------------------------------------------------------
def admit_pending(
    store,
    *,
    receipt_id: str,
    idem_key: str,
    miner_hotkey: str,
    challenge_id: str,
    dimacs_solution_sha256: str,
    dimacs_solution: str,
    submitted_at: str,
    received_at_iso: str,
    signature: str,
    epoch: int = 0,
    assignment_identity: str | None = None,
    queue_backpressure: dict[str, Any] | None = None,
) -> tuple[str, Any]:
    """Insert a pending attempt or return the existing one for the same idem key.

    Returns (outcome, row) where outcome is:
      * "created"  -> a new pending receipt was persisted (return 202)
      * "replayed" -> the same solution was already admitted; `row` is the existing
                      attempt (return its current receipt — pending OR final result)

    The whole thing is one transaction so concurrent identical submits cannot both
    create a row (the UNIQUE idempotency index is the backstop on top of the
    in-txn existence check).

    ``assignment_identity`` is only set on the pm-* lane: the worker regenerates
    that miner's own CNF from it (the public lane leaves it NULL and loads the CNF
    from lane_challenges instead)."""
    kind = challenge_kind(challenge_id)

    def _do(conn) -> tuple[str, Any]:
        # Defense in depth: live admission must NEVER match a per_miner_shadow
        # row, even if a shadow row somehow shared the live key. Shadow keys are
        # already namespaced (shadow_idempotency_key), but matching only
        # non-shadow rows here means a live retry post-cutover always creates a
        # fresh authoritative receipt instead of replaying a shadow twin.
        existing = _fetch_by_idem(conn, idem_key, exclude_shadow=True)
        if existing is not None:
            return ("replayed", existing)
        if queue_backpressure:
            pressure = queue_backpressure_decision(
                conn,
                now_iso=received_at_iso,
                max_pending=int(queue_backpressure.get("max_pending") or 0),
                max_worker_lag_secs=float(
                    queue_backpressure.get("max_worker_lag_secs") or 0.0),
                worker_stale_secs=float(
                    queue_backpressure.get("worker_stale_secs") or 120.0),
            )
            if pressure["limited"]:
                return ("backpressure", pressure)
        cur = conn.execute(
            "INSERT OR IGNORE INTO per_miner_attempts("
            "id, challenge_id, miner_hotkey, epoch, status, rejection_reason, "
            "dimacs_solution_sha256, submitted_at, recorded_at_iso, signature, "
            "idempotency_key, received_at_iso, challenge_kind, solution_body, "
            "assignment_identity, attempt_count) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (receipt_id, challenge_id, miner_hotkey, epoch, STATUS_PENDING,
             dimacs_solution_sha256, submitted_at, received_at_iso, signature,
             idem_key, received_at_iso, kind, dimacs_solution, assignment_identity),
        )
        if not cur.rowcount:
            # Lost an admission race on the UNIQUE idempotency_key. Re-read the
            # winner and treat as a replay (idempotent for the miner).
            existing = _fetch_by_idem(conn, idem_key, exclude_shadow=True)
            if existing is not None:
                return ("replayed", existing)
        created = _fetch_by_id(conn, receipt_id)
        return ("created", created)

    return store.write(_do)


def queue_backpressure_decision(
    conn,
    *,
    now_iso: str,
    max_pending: int = 0,
    max_worker_lag_secs: float = 0.0,
    worker_stale_secs: float = 120.0,
) -> dict[str, Any]:
    """Cheap default-off admission guard for async queues.

    Called inside the same transaction as durable admission after idempotency
    lookup, so an already-known receipt can still be replayed while new work is
    shed. Limits of 0 disable the corresponding check.
    """
    max_pending = max(0, int(max_pending or 0))
    max_worker_lag_secs = max(0.0, float(max_worker_lag_secs or 0.0))
    worker_stale_secs = max(1.0, float(worker_stale_secs or 120.0))
    active_workers = _active_worker_count(conn, now_iso, worker_stale_secs)
    row = conn.execute(
        "SELECT COUNT(*) AS pending, MIN(received_at_iso) AS oldest "
        "FROM per_miner_attempts WHERE status IN (?, ?, ?) "
        "AND (challenge_kind IS NULL OR challenge_kind != ?)",
        (*_OPEN_STATES, KIND_PER_MINER_SHADOW),
    ).fetchone()
    pending = int(row["pending"] or 0) if row is not None else 0
    oldest = row["oldest"] if row is not None else None
    lag = _age_secs(oldest, now_iso)
    reason = None
    if active_workers <= 0:
        reason = None
    elif max_pending > 0 and pending >= max_pending:
        reason = "submit_queue_pending_limit"
    elif (
        max_worker_lag_secs > 0.0
        and lag is not None
        and lag >= max_worker_lag_secs
    ):
        reason = "submit_queue_worker_lag_limit"
    return {
        "limited": reason is not None,
        "reason": reason,
        "pending": pending,
        "oldest_received_at": oldest,
        "worker_lag_secs": lag,
        "active_workers": active_workers,
        "worker_stale_secs": worker_stale_secs,
        "max_pending": max_pending,
        "max_worker_lag_secs": max_worker_lag_secs,
    }


def _active_worker_count(conn, now_iso: str, stale_secs: float) -> int:
    try:
        rows = conn.execute(
            "SELECT last_seen_at_iso FROM async_verify_workers "
            "WHERE running=1"
        ).fetchall()
    except Exception:
        return 0
    active = 0
    for row in rows:
        lag = _age_secs(row["last_seen_at_iso"], now_iso)
        if lag is not None and lag <= stale_secs:
            active += 1
    return active


def _fetch_by_idem(conn, idem_key: str, *, exclude_shadow: bool = False):
    if exclude_shadow:
        cur = conn.execute(
            "SELECT * FROM per_miner_attempts WHERE idempotency_key=? "
            "AND (challenge_kind IS NULL OR challenge_kind != ?) LIMIT 1",
            (idem_key, KIND_PER_MINER_SHADOW))
        return cur.fetchone()
    cur = conn.execute(
        "SELECT * FROM per_miner_attempts WHERE idempotency_key=? LIMIT 1",
        (idem_key,))
    return cur.fetchone()


def _fetch_by_id(conn, receipt_id: str):
    cur = conn.execute(
        "SELECT * FROM per_miner_attempts WHERE id=? LIMIT 1", (receipt_id,))
    return cur.fetchone()


def get_receipt(store, receipt_id: str) -> dict[str, Any] | None:
    rows = store.query(
        "SELECT * FROM per_miner_attempts WHERE id=? LIMIT 1", (receipt_id,))
    if not rows:
        return None
    return receipt_from_row(rows[0])


# ---------------------------------------------------------------------------
# Worker claim: take the oldest pending attempts (fairness = received_at order).
# ---------------------------------------------------------------------------
def claim_pending(
    store, *, worker_id: str, now_iso: str, lock_deadline_iso: str,
    batch_size: int = 1,
) -> list[Any]:
    """Atomically move up to `batch_size` claimable attempts to `verifying` and
    return them, ordered by received_at (fairness). Postgres uses FOR UPDATE SKIP
    LOCKED; SQLite relies on the global write lock for the same exclusivity."""
    is_pg = getattr(store, "backend", "sqlite") == "postgres"

    # Claimable = fresh pending/failed_retryable rows, PLUS verifying rows whose
    # lock has expired (a crashed worker — crash safety). The locked_until_iso<=now
    # guard means a live verifying row (future lock) is never stolen.
    def _do(conn) -> list[Any]:
        if is_pg:
            cur = conn.execute(
                "UPDATE per_miner_attempts SET status=?, locked_by=?, "
                "locked_until_iso=?, attempt_count=attempt_count+1, "
                "verified_at_iso=NULL, recorded_at_iso=? "
                "WHERE id IN ("
                "  SELECT id FROM per_miner_attempts "
                "  WHERE status IN (?, ?, ?) "
                "    AND (next_attempt_at_iso IS NULL OR next_attempt_at_iso <= ?) "
                "    AND (locked_until_iso IS NULL OR locked_until_iso <= ?) "
                "  ORDER BY received_at_iso, id "
                "  LIMIT ? FOR UPDATE SKIP LOCKED"
                ") RETURNING *",
                (STATUS_VERIFYING, worker_id, lock_deadline_iso, now_iso,
                 STATUS_PENDING, STATUS_FAILED_RETRYABLE, STATUS_VERIFYING,
                 now_iso, now_iso, batch_size),
            )
            return list(cur.fetchall())
        # SQLite: select then update under the single write lock.
        rows = list(conn.execute(
            "SELECT id FROM per_miner_attempts "
            "WHERE status IN (?, ?, ?) "
            "  AND (next_attempt_at_iso IS NULL OR next_attempt_at_iso <= ?) "
            "  AND (locked_until_iso IS NULL OR locked_until_iso <= ?) "
            "ORDER BY received_at_iso, id LIMIT ?",
            (STATUS_PENDING, STATUS_FAILED_RETRYABLE, STATUS_VERIFYING,
             now_iso, now_iso, batch_size)))
        claimed = []
        for r in rows:
            rid = r["id"]
            conn.execute(
                "UPDATE per_miner_attempts SET status=?, locked_by=?, "
                "locked_until_iso=?, attempt_count=attempt_count+1, "
                "verified_at_iso=NULL WHERE id=?",
                (STATUS_VERIFYING, worker_id, lock_deadline_iso, rid))
            got = conn.execute(
                "SELECT * FROM per_miner_attempts WHERE id=?", (rid,)).fetchone()
            claimed.append(got)
        return claimed

    return store.write(_do)


# ---------------------------------------------------------------------------
# Worker finalize: verify one claimed attempt and record the terminal result.
# ---------------------------------------------------------------------------
def finalize_attempt(
    store,
    row: Any,
    *,
    now_iso: str,
    load_cnf: Callable[[str], str | None],
    verify_dimacs: Callable[[str, str], Any],
    accept_public: Callable[..., tuple[str | None, int | None, float | None, str]],
    record_event: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Verify a single claimed `verifying` attempt and record ranked/rejected.

    Injected callables keep this module free of app/scoring/rows coupling so it is
    unit-testable in isolation:
      * load_cnf(challenge_id)            -> cnf_text or None
      * verify_dimacs(cnf_text, body)     -> SolveCheck (.ok/.assignment/.rejection_reason)
      * accept_public(receipt_id, attempt_row, check, now_iso)
            -> (err, rank, weighted_score, eval_run_id); err is None on success or
               a legacy rejection vocabulary string (e.g. "already_solved").

    Returns a small result dict for metrics/logging."""
    receipt_id = row["id"]
    challenge_id = str(row["challenge_id"])
    body = row["solution_body"]

    if body is None:
        # No durable body — cannot verify. Mark terminal-rejected so the receipt
        # resolves rather than looping forever.
        _mark_rejected(store, receipt_id, "missing_solution_body", now_iso)
        return {"receipt_id": receipt_id, "outcome": "rejected",
                "reason": "missing_solution_body"}

    cnf_text = load_cnf(challenge_id)
    if cnf_text is None:
        _mark_rejected(store, receipt_id, "challenge_not_active", now_iso)
        return {"receipt_id": receipt_id, "outcome": "rejected",
                "reason": "challenge_not_active"}

    check = verify_dimacs(cnf_text, body)
    if not check.ok:
        _mark_rejected(store, receipt_id, check.rejection_reason, now_iso)
        if record_event is not None:
            record_event("rejected", check.rejection_reason, challenge_id=challenge_id)
        return {"receipt_id": receipt_id, "outcome": "rejected",
                "reason": check.rejection_reason}

    err, rank, ws, eval_run_id = accept_public(receipt_id, row, check, now_iso)
    if err is not None:
        # Legacy reject vocabulary (already_solved / challenge_not_active /
        # challenge_already_locked / replayed_signature). Terminal — not retried.
        _mark_rejected(store, receipt_id, err, now_iso)
        if record_event is not None:
            record_event("rejected", err, challenge_id=challenge_id)
        return {"receipt_id": receipt_id, "outcome": "rejected", "reason": err}

    if record_event is not None:
        record_event("accepted", "ranked", challenge_id=challenge_id)
    return {"receipt_id": receipt_id, "outcome": "ranked", "rank": rank,
            "weighted_score": ws}


def _mark_rejected(store, receipt_id: str, reason: str, now_iso: str) -> None:
    def _do(conn):
        conn.execute(
            "UPDATE per_miner_attempts SET status=?, rejection_reason=?, "
            "verified_at_iso=?, recorded_at_iso=?, locked_by=NULL, "
            "locked_until_iso=NULL WHERE id=?",
            (STATUS_REJECTED, reason, now_iso, now_iso, receipt_id))
    store.write(_do)


# ---------------------------------------------------------------------------
# Per-miner (pm-*) worker finalize.
# ---------------------------------------------------------------------------
def finalize_pm_attempt(
    store,
    row: Any,
    *,
    now_iso: str,
    resolve_and_verify: Callable[[Any], tuple[bool, str | None, Any]],
    accept_pm: Callable[..., str | None],
    record_event: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Verify one claimed pm-* ``verifying`` attempt and record ranked/rejected.

    The pm-* lane regenerates the miner's OWN CNF (it is not stored in
    lane_challenges) and runs both the DIMACS satisfaction check and the
    anti-copy ownership check. Both are injected so this module stays free of the
    per_miner/app coupling:
      * resolve_and_verify(attempt_row)
            -> (ok, reason, check) where check carries the verified assignment
               used to build the witness/answer hashes. A False ok is a terminal
               reject (bad solution / not this miner's instance / expired epoch).
      * accept_pm(receipt_id, attempt_row, check, now_iso)
            -> None on success, or a legacy pm reject vocabulary string
               (replayed_signature / already_solved). The accept itself is one
               atomic txn (signature burn + distinct-solver claim) so a reclaim of
               a crashed attempt can NEVER double-pay.

    Returns a small result dict for metrics/logging. The live ledger is the single
    source of truth scoring already reads — this only moves WHEN it is written."""
    receipt_id = row["id"]
    challenge_id = str(row["challenge_id"])

    if row["solution_body"] is None:
        _mark_rejected(store, receipt_id, "missing_solution_body", now_iso)
        return {"receipt_id": receipt_id, "outcome": "rejected",
                "reason": "missing_solution_body"}

    ok, reason, check = resolve_and_verify(row)
    if not ok:
        _mark_rejected(store, receipt_id, reason, now_iso)
        if record_event is not None:
            record_event("rejected", reason, challenge_id=challenge_id)
        return {"receipt_id": receipt_id, "outcome": "rejected", "reason": reason}

    err = accept_pm(receipt_id, row, check, now_iso)
    if err is not None:
        _mark_rejected(store, receipt_id, err, now_iso)
        if record_event is not None:
            record_event("rejected", err, challenge_id=challenge_id)
        return {"receipt_id": receipt_id, "outcome": "rejected", "reason": err}

    if record_event is not None:
        record_event("accepted", "ranked", challenge_id=challenge_id)
    return {"receipt_id": receipt_id, "outcome": "ranked"}


def finalize_pm_shadow(
    store,
    row: Any,
    *,
    now_iso: str,
    resolve_and_verify: Callable[[Any], tuple[bool, str | None, Any]],
    log_divergence: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Shadow finalize for a pm-* attempt: compute the async terminal verdict and
    record it in the ``shadow_*`` columns ONLY. The inline synchronous path already
    paid (or rejected) this submission authoritatively; this never writes to the
    live solve/payout ledger and never emits feed rows.

    Divergence (async verdict != inline verdict) is logged so go-live can prove the
    async path reaches byte-identical accept/reject decisions before cutover."""
    receipt_id = row["id"]
    challenge_id = str(row["challenge_id"])

    if row["solution_body"] is None:
        shadow_status, shadow_reason = STATUS_REJECTED, "missing_solution_body"
    else:
        ok, reason, _check = resolve_and_verify(row)
        if ok:
            shadow_status, shadow_reason = STATUS_RANKED, None
        else:
            shadow_status, shadow_reason = STATUS_REJECTED, reason

    inline_status = str(row["status"])  # the verdict the inline path recorded
    # The inline path may have stored a non-terminal status if the row is the
    # shadow twin; for shadow rows the inline verdict is carried on the twin's
    # source row. We compare against the inline verdict the caller stamped on the
    # shadow row's rejection_reason / via the inline_outcome closure instead.
    diverged = _shadow_diverges(row, shadow_status, shadow_reason)

    def _do(conn):
        conn.execute(
            "UPDATE per_miner_attempts SET shadow_status=?, shadow_rejection_reason=?, "
            "status=?, verified_at_iso=?, recorded_at_iso=?, solution_body=NULL, "
            "locked_by=NULL, locked_until_iso=NULL WHERE id=?",
            (shadow_status, shadow_reason, STATUS_RANKED if shadow_status == STATUS_RANKED
             else STATUS_REJECTED, now_iso, now_iso, receipt_id))
    store.write(_do)

    if diverged and log_divergence is not None:
        log_divergence(
            challenge_id=challenge_id, receipt_id=receipt_id,
            inline_status=str(row["rejection_reason"] or "ranked"),
            async_status=shadow_status, async_reason=shadow_reason)
    return {"receipt_id": receipt_id, "outcome": "shadow",
            "shadow_status": shadow_status, "diverged": diverged}


def _shadow_diverges(row: Any, async_status: str, async_reason: str | None) -> bool:
    """Compare the async shadow verdict to the inline verdict the submit handler
    stamped on the shadow row. The shadow row stores the inline outcome in
    ``rejection_reason`` ("__ranked__" for an inline accept, else the inline reject
    reason). Divergence = different accept/reject decision OR different reason."""
    inline_marker = row["rejection_reason"]
    inline_accepted = inline_marker == "__ranked__"
    async_accepted = async_status == STATUS_RANKED
    if inline_accepted != async_accepted:
        return True
    if not async_accepted:
        # both rejected: a different reason is still a (soft) divergence worth logging
        return (inline_marker or "") != (async_reason or "")
    return False


# ---------------------------------------------------------------------------
# Queue visibility (admin metrics).
# ---------------------------------------------------------------------------
def queue_metrics(store, *, now_iso: str, kinds: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Pending-queue snapshot for the submit-metrics admin surface.

    Returns pending count, the oldest pending received_at, and worker lag (seconds
    between now and the oldest pending received_at) per challenge_kind, so an
    operator can see whether the drain worker is keeping up before/at cutover."""
    where_kind = ""
    params: list[Any] = [STATUS_PENDING, STATUS_VERIFYING, STATUS_FAILED_RETRYABLE]
    if kinds:
        where_kind = " AND challenge_kind IN (%s)" % ",".join("?" for _ in kinds)
        params.extend(kinds)
    rows = store.query(
        "SELECT challenge_kind AS kind, status, COUNT(*) AS pending, "
        "MIN(received_at_iso) AS oldest "
        "FROM per_miner_attempts WHERE status IN (?, ?, ?)" + where_kind +
        " GROUP BY challenge_kind, status",
        tuple(params))
    by_kind: dict[str, Any] = {}
    total_pending = 0
    by_status = {status: 0 for status in _OPEN_STATES}
    oldest_by_status: dict[str, str | None] = {status: None for status in _OPEN_STATES}
    overall_oldest: str | None = None
    for r in rows:
        kind = str(r["kind"]) if r["kind"] is not None else "unknown"
        status = str(r["status"] or STATUS_PENDING)
        pending = int(r["pending"] or 0)
        oldest = r["oldest"]
        total_pending += pending
        by_status[status] = int(by_status.get(status, 0)) + pending
        if oldest is not None:
            cur_oldest = oldest_by_status.get(status)
            if cur_oldest is None or oldest < cur_oldest:
                oldest_by_status[status] = oldest
        if oldest is not None and (overall_oldest is None or oldest < overall_oldest):
            overall_oldest = oldest
        if kind not in by_kind:
            by_kind[kind] = {
                "pending": 0,
                "oldest_received_at": None,
                "worker_lag_secs": None,
                "by_status": {},
                "oldest_by_status": {},
            }
        bucket = by_kind[kind]
        bucket["pending"] += pending
        bucket["by_status"][status] = int(bucket["by_status"].get(status, 0)) + pending
        bucket["oldest_by_status"][status] = oldest
        cur_bucket_oldest = bucket["oldest_received_at"]
        if oldest is not None and (cur_bucket_oldest is None or oldest < cur_bucket_oldest):
            bucket["oldest_received_at"] = oldest
            bucket["worker_lag_secs"] = _age_secs(oldest, now_iso)
    for bucket in by_kind.values():
        for status in _OPEN_STATES:
            bucket["by_status"].setdefault(status, 0)
            bucket["oldest_by_status"].setdefault(status, None)
    return {
        "total_pending": total_pending,
        "by_status": by_status,
        "oldest_by_status": oldest_by_status,
        "oldest_received_at": overall_oldest,
        "worker_lag_secs": _age_secs(overall_oldest, now_iso),
        "by_kind": by_kind,
    }


def queue_rates(store, *, since_iso: str, window_secs: float) -> dict[str, Any]:
    """Admission and terminal-result rates for the async submit queue."""
    win = max(1.0, float(window_secs))
    admitted = _kind_counts(
        store.query(
            "SELECT challenge_kind AS kind, COUNT(*) AS n "
            "FROM per_miner_attempts "
            "WHERE received_at_iso IS NOT NULL AND received_at_iso > ? "
            "AND challenge_kind IS NOT NULL GROUP BY challenge_kind",
            (since_iso,),
        )
    )
    terminal_rows = store.query(
        "SELECT challenge_kind AS kind, status, COUNT(*) AS n "
        "FROM per_miner_attempts "
        "WHERE verified_at_iso IS NOT NULL AND verified_at_iso > ? "
        "AND challenge_kind IS NOT NULL "
        "GROUP BY challenge_kind, status",
        (since_iso,),
    )
    live_ranked = live_rejected = shadow_ranked = shadow_rejected = 0
    ranked_by_kind: dict[str, int] = {}
    rejected_by_kind: dict[str, int] = {}
    for r in terminal_rows:
        kind = str(r["kind"])
        status = str(r["status"])
        n = int(r["n"] or 0)
        if status == STATUS_RANKED:
            ranked_by_kind[kind] = ranked_by_kind.get(kind, 0) + n
            if kind == KIND_PER_MINER_SHADOW:
                shadow_ranked += n
            else:
                live_ranked += n
        elif status == STATUS_REJECTED:
            rejected_by_kind[kind] = rejected_by_kind.get(kind, 0) + n
            if kind == KIND_PER_MINER_SHADOW:
                shadow_rejected += n
            else:
                live_rejected += n
    live_admitted = sum(
        n for kind, n in admitted.items() if kind != KIND_PER_MINER_SHADOW)
    shadow_admitted = admitted.get(KIND_PER_MINER_SHADOW, 0)
    return {
        "window_secs": win,
        "admitted_in_window": live_admitted,
        "admitted_per_sec": round(live_admitted / win, 4),
        "ranked_in_window": live_ranked,
        "ranked_per_sec": round(live_ranked / win, 4),
        "rejected_in_window": live_rejected,
        "rejected_per_sec": round(live_rejected / win, 4),
        "accepted_in_window": live_ranked,
        "accepted_per_sec": round(live_ranked / win, 4),
        "shadow_admitted_in_window": shadow_admitted,
        "shadow_admitted_per_sec": round(shadow_admitted / win, 4),
        "shadow_ranked_in_window": shadow_ranked,
        "shadow_ranked_per_sec": round(shadow_ranked / win, 4),
        "shadow_rejected_in_window": shadow_rejected,
        "shadow_rejected_per_sec": round(shadow_rejected / win, 4),
        "shadow_accepted_in_window": shadow_ranked,
        "shadow_accepted_per_sec": round(shadow_ranked / win, 4),
        "admitted_by_kind": admitted,
        "ranked_by_kind": ranked_by_kind,
        "rejected_by_kind": rejected_by_kind,
    }


def _kind_counts(rows: list[Any]) -> dict[str, int]:
    return {
        str(r["kind"] if r["kind"] is not None else "unknown"): int(r["n"] or 0)
        for r in rows
    }


def record_worker_heartbeat(
    store,
    *,
    worker_id: str,
    service_role: str,
    now_iso: str,
    event: str,
    processed: int = 0,
    error: str | None = None,
) -> None:
    """Persist worker visibility for submit-metrics across split services."""
    processed = max(0, int(processed or 0))
    running = 0 if event in {"stopped", "cancelled"} else 1
    last_tick_at = now_iso if event in {"tick", "error"} else None
    last_processed_at = now_iso if processed > 0 else None

    def _do(conn):
        conn.execute(
            "INSERT INTO async_verify_workers("
            "worker_id, service_role, started_at_iso, last_seen_at_iso, "
            "last_tick_at_iso, last_processed_at_iso, last_error, "
            "processed_total, last_batch_size, running) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(worker_id) DO UPDATE SET "
            "service_role=excluded.service_role, "
            "last_seen_at_iso=excluded.last_seen_at_iso, "
            "last_tick_at_iso=COALESCE(excluded.last_tick_at_iso, async_verify_workers.last_tick_at_iso), "
            "last_processed_at_iso=COALESCE(excluded.last_processed_at_iso, async_verify_workers.last_processed_at_iso), "
            "last_error=excluded.last_error, "
            "processed_total=async_verify_workers.processed_total + excluded.processed_total, "
            "last_batch_size=excluded.last_batch_size, "
            "running=excluded.running",
            (worker_id, service_role, now_iso, now_iso, last_tick_at,
             last_processed_at, error, processed, processed, running),
        )
    store.write(_do)


def worker_metrics(store, *, now_iso: str, stale_secs: float = 120.0) -> dict[str, Any]:
    """Visible async-worker heartbeat state for operator metrics."""
    try:
        rows = store.query(
            "SELECT worker_id, service_role, started_at_iso, last_seen_at_iso, "
            "last_tick_at_iso, last_processed_at_iso, last_error, "
            "processed_total, last_batch_size, running "
            "FROM async_verify_workers ORDER BY last_seen_at_iso DESC")
    except Exception:
        return {"active_workers": 0, "workers": [], "stale_after_secs": stale_secs}
    workers = []
    active = 0
    for r in rows:
        lag = _age_secs(r["last_seen_at_iso"], now_iso)
        is_active = bool(r["running"]) and lag is not None and lag <= stale_secs
        active += 1 if is_active else 0
        workers.append({
            "worker_id": str(r["worker_id"]),
            "service_role": str(r["service_role"] or ""),
            "started_at": r["started_at_iso"],
            "last_seen_at": r["last_seen_at_iso"],
            "last_tick_at": r["last_tick_at_iso"],
            "last_processed_at": r["last_processed_at_iso"],
            "last_seen_lag_secs": lag,
            "active": is_active,
            "running": bool(r["running"]),
            "processed_total": int(r["processed_total"] or 0),
            "last_batch_size": int(r["last_batch_size"] or 0),
            "last_error": r["last_error"],
        })
    return {
        "active_workers": active,
        "workers": workers,
        "stale_after_secs": stale_secs,
    }


def _age_secs(then_iso: str | None, now_iso: str) -> float | None:
    if not then_iso:
        return None
    a = _parse_iso(then_iso)
    b = _parse_iso(now_iso)
    if a is None or b is None:
        return None
    return max(0.0, round(b - a, 3))


def _parse_iso(ts: str) -> float | None:
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None
