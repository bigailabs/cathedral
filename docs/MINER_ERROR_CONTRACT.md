# Cathedral Miner Error Contract

This is the canonical list of HTTP error responses a miner (or a miner's
submit/fetch client) can receive from the Cathedral SAT publisher and its edge
router, what each one means, and the **correct client action** for each.

Every code here is emitted by real code paths in
`scaffold/publisher/app.py` (submit / challenge / CNF routes),
`scaffold/publisher/ratelimit.py` (pre-auth abuse shedding), the edge router
`deploy/edge-router/worker.mjs` (`*_origin_unavailable`), or is the
**planned** durable-admission code from the reliability plan (`503
submit_admission_unavailable`, marked below). Codes that exist purely for
operators/validators/attestation (e.g. weight-feed `503 no vector available`,
admin/attest token errors) are **out of scope** for the miner contract and are
not listed.

## Conventions

- The machine-readable reason is also returned in the
  `X-Cathedral-Rejection-Reason` response header on submit-path errors. Clients
  SHOULD branch on that header (or the JSON `detail`) rather than parsing prose.
- When a `Retry-After` header is present, it is in **seconds**. Honor it.
- `4xx` with a `solution_*` / `challenge_*` reason is a **client/work** problem:
  do not blindly retry the same bytes.
- `429` / `503` / `504` are **server load / availability** problems: retry with
  backoff (and, where noted, a fresh signature / fresh challenge).

## Error table

| HTTP | Reason (`X-Cathedral-Rejection-Reason` / `detail`) | Meaning | Correct client action |
|---|---|---|---|
| **429** | `submit_busy_retry` | Global submit concurrency gate is full; the request was shed before any work. Sent with `Retry-After: 1`. | Sleep `Retry-After` + jitter, then retry the **same** solution with a **fresh signature** (re-sign with a current `submitted_at`). Use exponential backoff if it persists. |
| **429** | `per_miner_busy_retry` | Per-miner challenge/CNF **read** concurrency gate is full; the fetch was shed before any work. Sent with `Retry-After: 1`. | Sleep `Retry-After` + jitter, then retry the read. Not a signing/solution bug. |
| **429** | `receipt_poll_busy_retry` | V2 receipt-poll concurrency gate is full (`GET /v2/agents/submit-bitset/receipts/{id}`); the poll was shed before touching the database. Sent with `Retry-After: 1`. | Sleep `Retry-After` + jitter, then poll again. Your submit was already durably admitted; slow the poll loop rather than tightening it. |
| **429** | `rate_limited` | Per-`(hotkey, challenge)` minimum-interval limit; you submitted to the same challenge too fast. `Retry-After` = configured min interval (seconds). | Wait `Retry-After` + jitter before re-submitting to that challenge. Do not spin. |
| **429** | `ip_rate_limited` | Optional pre-auth abuse limiter shed this IP across hot SAT submit / per-miner CNF/listing paths. `Retry-After` escalates while the IP keeps exceeding the window. | Honor `Retry-After` + jitter. Slow the client globally; do not switch endpoints in a tight loop. |
| **429** | `actor_rate_limited` | Optional pre-auth abuse limiter shed the claimed `X-Cathedral-Hotkey` across hot SAT submit / per-miner CNF/listing paths. The claimed hotkey is only an abuse hint before signature verification, not a payout/fairness identity. | Honor `Retry-After` + jitter. Slow that actor's fetch/submit loop; re-sign submit retries. |
| **503** | `submit_queue_backpressure` | Optional durable-admission queue guard is shedding new submits because pending receipts or worker lag exceeded configured limits. Idempotent receipt replays may still be served. | Honor `Retry-After` + jitter and slow intake. This is server backpressure, not a solution bug. Re-sign submit retries. |
| **503** | `async_worker_unavailable` | Async admission is enabled and required (`CATHEDRAL_SUBMIT_ASYNC_REQUIRE_WORKER`), but no verifier worker has a live heartbeat, so a durable 202 receipt would never drain to payout. The submit fails closed instead of queueing. Sent with `Retry-After`. | Honor `Retry-After` + jitter and retry the **same** solution with a **fresh signature**. This is a transient server-capacity problem, not a solution bug. |
| **503** | `v2_submit_backpressure` | V2 verify-queue backpressure: pending unverified submissions (or oldest pending age) exceeded configured limits, so new V2 bitset submits are shed while the verifier drains. Idempotent replays of already-admitted submits still return their receipt. | Honor `Retry-After` + jitter and slow submit intake. Not a solution bug. Re-sign retries. |
| **503** | `v2_db_unavailable_retry` | The V2 origin database briefly could not serve the submit/receipt path (connection pool or connect pressure). The request was shed cleanly instead of failing with a raw 500. | Honor `Retry-After` + jitter, then retry (re-sign if it was a submit). Transient server-side pressure, not a rejection of your work. |
| **503** | `submit_admission_unavailable` | **Planned (Phase 4 durable admission, not yet emitted by current code).** Admission layer is in emergency shedding; the server cannot durably accept submits right now. | Treat like `429` but back off harder: exponential backoff with jitter, re-sign on retry. Not a solution bug. |
| **409** | `challenge_not_active` | No active lane challenge matches `challenge_id` (unknown or rotated out). | Refetch the current challenge (`current-challenge` / `active-challenges`) and solve the new one. Do not retry the old id. |
| **409** | `challenge_already_locked` | The challenge exists but is no longer `active` (window closed / locked). | Refetch the current challenge and move on. Retrying this id will keep failing. |
| **409** | `already_solved` | This (per-miner) challenge/tier was already solved by you; the solve slot is taken. | Stop submitting to it. Fetch your next assignment / next challenge. |
| **409** | `replayed_signature` | The submission signature was already seen (replay / duplicate POST). | Do **not** resend the same signed bytes. If you genuinely need to retry, re-sign with a fresh `submitted_at` and resubmit once. |
| **401** | `invalid hotkey signature` | sr25519 signature did not verify for the claimed hotkey/body. | Fix signing: sign the exact canonical claim (hotkey, `submitted_at`, `challenge_id`, `dimacs_solution_sha256`) with the correct hotkey. Do not retry until signing is fixed. |
| **401** | `missing X-Cathedral-Submitted-At` | Required `X-Cathedral-Submitted-At` header absent. | Add the header (ISO/ms timestamp you signed over) and resubmit. |
| **400** | `submitted_at outside acceptable clock-skew window` | Your `submitted_at` is too far from server time. | Sync your clock (NTP), re-sign with a current timestamp, resubmit. |
| **400** | `invalid submitted_at` | `submitted_at` is malformed/unparseable. | Send a valid timestamp in the expected format, re-sign, resubmit. |
| **400** | `solution_missing_status` | DIMACS output lacks the `s` status line. | Fix solver output to emit `s SATISFIABLE` + `v` assignment. New attempt only after fixing. |
| **400** | `solution_status_unsatisfiable` | You reported `s UNSATISFIABLE` for a satisfiable challenge. | The instance is SAT — recompute a satisfying assignment. |
| **400** | `solution_unknown_status` | Status line is neither SAT nor UNSAT (e.g. `UNKNOWN`). | Run the solver to completion and report a real status. |
| **400** | `solution_non_integer_literal` | A `v`-line token is not an integer literal. | Emit only integer literals (signed var ids), `0`-terminated. |
| **400** | `solution_variable_out_of_range` | An assigned variable id is outside the CNF's declared range. | Refetch the CNF; assign only variables `1..n`. |
| **400** | `solution_contradictory_assignment` | Same variable assigned both true and false. | Deduplicate/repair the assignment so each var has one polarity. |
| **400** | `solution_incomplete_assignment` | Not all variables are assigned. | Provide a full assignment over all `n` variables. |
| **400** | `solution_unsatisfied` | The assignment does not satisfy every clause. | Re-solve; verify locally against the fetched CNF before submitting. |
| **413** | `solution_too_large` | The submitted DIMACS body exceeds the per-submit byte cap (`CATHEDRAL_PM_SUBMIT_MAX_SOLUTION_BYTES`). Only emitted by the per-miner durable-admission lane (default-off) as a cheap inline guard before the body is ever persisted/queued. | Send a correctly-sized DIMACS assignment (one `v`-line set over the CNF's variables). A pathological/oversized body is a client bug — fix the solver output, do not retry as-is. |
| **404** | `not found` (CNF) | `GET /v1/challenges/{id}/cnf?t=<token>` failed the opaque gate: bad/expired token, or unknown challenge. **Intentionally opaque** — no leak of which. | Refetch a fresh CNF token via the challenge/`active-cnf` route (tokens are short-lived, ~120s) and retry. If it keeps 404-ing with a fresh token, the challenge is gone — refetch the current challenge. |
| **404** | `no_active_challenge` | Requested tier/challenge has no active instance. | Poll `current-challenge` / `active-challenges` until one is live. |
| **404** | `receipt_not_found` | `GET /v1/agents/receipts/{receipt_id}` was given a receipt id the server does not know. Only relevant when durable/async submit admission is enabled (default-off); a synchronous submit never issues a receipt id to look up. | Use the `receipt_id` returned by your `202` submit admission. Do not poll receipt ids you did not receive. If a known id 404s, the receipt was never durably admitted — resubmit. |
| **504** | `read_origin_unavailable` | Edge router could not reach the **read** origin for a challenge/board fetch. **Server-side**, not your solution. | Retry the read with backoff + jitter; the edge may already be serving slightly-stale data. Not a signing/solution bug. |
| **504** | `<role>_origin_unavailable` (e.g. `submit_origin_unavailable`) | Edge router could not reach that role's origin. **Server-side.** | Retry with backoff + jitter. Do not treat as a rejection of your work; re-sign on retry if it was a submit. |

### Notes on retry safety

- **Re-sign on every retry** of a submit. Signatures are single-use
  (`replayed_signature` burns them), and `submitted_at` must stay inside the
  clock-skew window. A naive "resend the same bytes" retry will turn a `429`
  into a `409 replayed_signature`.
- **Refetch, don't retry**, on `409 challenge_not_active` /
  `challenge_already_locked` and on a CNF `404` with a stale token — the thing
  you're holding is gone or expired.
- **Never retry** a `400 solution_*` or `401 invalid hotkey signature` with the
  same input — fix the cause first.

## Reference client: retry + backoff

This snippet shows the intended behavior: honor `Retry-After`, use bounded
exponential backoff with full jitter for load/availability codes, re-sign on
each attempt, refetch on `challenge`/CNF errors, and fail fast on
work/signing errors.

```python
import random
import time

# Codes worth retrying after a delay (server load / availability).
RETRYABLE = {429, 503, 504}
# Reasons that mean "your challenge/CNF is gone or stale" -> refetch, then retry.
REFETCH_REASONS = {
    "challenge_not_active", "challenge_already_locked",  # 409
    "no_active_challenge",                               # 404
}

def submit_with_retry(client, solve, *, max_attempts=6, base=1.0, cap=30.0):
    """`solve` returns a freshly-signed submit payload (re-signed each call)."""
    for attempt in range(max_attempts):
        payload = solve()  # MUST re-sign: fresh submitted_at + signature
        resp = client.post_submit(payload)

        if resp.status_code == 200:
            return resp.json()

        reason = resp.headers.get("X-Cathedral-Rejection-Reason", "")

        # Work / signing errors: do not retry the same bytes.
        if resp.status_code in (400, 401):
            raise SubmitRejected(resp.status_code, reason or resp.text)

        # 409: refetch a fresh challenge/CNF, then let the loop retry.
        if resp.status_code == 409:
            if reason in REFETCH_REASONS:
                client.refetch_current_challenge()
                continue
            # already_solved / replayed_signature: nothing useful to retry.
            raise SubmitRejected(409, reason)

        # CNF gone/stale (opaque 404 on the CNF route): refetch token + body.
        if resp.status_code == 404 and reason == "":
            client.refetch_cnf()
            continue

        if resp.status_code in RETRYABLE:
            # Honor Retry-After (seconds) when present, else exp backoff.
            retry_after = resp.headers.get("Retry-After")
            if retry_after is not None:
                delay = float(retry_after)
            else:
                delay = min(cap, base * (2 ** attempt))
            # Full jitter avoids thundering-herd re-submits.
            time.sleep(delay * random.random() + delay * 0.5)
            continue

        raise SubmitRejected(resp.status_code, reason or resp.text)

    raise SubmitExhausted(max_attempts)


class SubmitRejected(Exception):
    def __init__(self, status, reason):
        super().__init__(f"{status}: {reason}")
        self.status, self.reason = status, reason


class SubmitExhausted(Exception):
    pass
```

Key points the snippet encodes:

1. `429` / `503` / `504` -> backoff with jitter, honoring `Retry-After`.
2. `solve()` is called **every** attempt so each retry carries a fresh
   `submitted_at` and signature (avoids `replayed_signature`).
3. `409 challenge_*` and stale-CNF `404` trigger a **refetch**, not a blind
   retry.
4. `400 solution_*` and `401` fail fast — they are bugs in the work/signing,
   not transient load.
