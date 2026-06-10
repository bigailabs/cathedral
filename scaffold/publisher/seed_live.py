"""G1 — Live-state seeder for the Cathedral v4 thin publisher.

Populate the thin publisher's store from the LIVE public surface only (no DB
file access — everything comes from https://api.cathedral.computer):

  (a) pull the last N days of signed rows from /v1/leaderboard/recent using the
      tuple cursor and store them VERBATIM, including their existing
      cathedral_signature. Re-serving rows already signed by the production key
      is valid — we do NOT re-sign. (insert_row uses INSERT OR IGNORE, so a
      re-run never duplicates or mutates a row.)
  (b) advance a durable watermark to the newest live (ran_at, id) so a resumed
      run continues from where it left off and a post-swap publisher's cursor is
      continuous with the live feed (avoids validator double-pull / missed rows).
  (c) mirror the active board from /v1/synthetic-boolean/active-challenges into
      lane_challenges as metadata-only rows (cnf_source='external', cnf_url set
      to the live active-cnf path). CNF bodies are NOT copied — they are fetched
      lazily from live or, post-swap, replaced by locally-minted challenges (G2).

Idempotent + resumable: safe to run repeatedly; it resumes from the stored
watermark and skips rows/challenges already present. --dry-run prints what would
be seeded without writing. Respects live per-IP rate limits via gentle pacing
(a configurable sleep between page pulls).

Usage:
    python -m scaffold.publisher.seed_live --db publisher.db --days 7
    python -m scaffold.publisher.seed_live --db publisher.db --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import Store

DEFAULT_BASE = "https://api.cathedral.computer"
WATERMARK_RAN_AT = "live_seed_watermark_ran_at"
WATERMARK_ID = "live_seed_watermark_id"


def _iso_days_ago(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _http_json(url: str, timeout: int = 90, retries: int = 3) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "cathedral-thin-seeder/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:  # network blip / read timeout — back off and retry
            last = e
            time.sleep(2 * (attempt + 1))
    raise last  # type: ignore[misc]


def _tuple_gt(a_ran: str, a_id: str, b_ran: str | None, b_id: str | None) -> bool:
    """Strict (ran_at, id) > (b_ran, b_id) — same ordering as the feed cursor."""
    if b_ran is None:
        return True
    if a_ran != b_ran:
        return a_ran > b_ran
    return a_id > (b_id or "")


def seed_rows(
    store: Store,
    *,
    base_url: str,
    days: int,
    page_limit: int,
    pace_secs: float,
    dry_run: bool,
    timeout: int = 90,
    max_pages: int = 100_000,
    log=print,
) -> dict[str, Any]:
    """Pull signed rows from the live feed (tuple cursor) and store verbatim.

    Resumes from the durable watermark if present, else from `days` ago. Returns
    a summary dict. In --dry-run nothing is written and the watermark is left
    untouched."""
    cur_ran_at = store.get_seed_state(WATERMARK_RAN_AT)
    cur_id = store.get_seed_state(WATERMARK_ID)
    if cur_ran_at is None:
        cur_ran_at = _iso_days_ago(days)
        cur_id = None
        log(f"  no watermark — starting from {days}d ago: {cur_ran_at}")
    else:
        log(f"  resuming from watermark: ({cur_ran_at}, {cur_id})")

    pulled = inserted = pages = 0
    newest_ran_at, newest_id = cur_ran_at, cur_id
    feed_url = f"{base_url}/v1/leaderboard/recent"

    for _ in range(max_pages):
        params = f"?since={cur_ran_at}&limit={page_limit}"
        if cur_id:
            params += f"&since_ran_at={cur_ran_at}&since_id={cur_id}"
        page = _http_json(feed_url + params, timeout=timeout)
        items = page.get("items", [])
        if not items:
            break
        pages += 1
        log(f"  page {pages}: {len(items)} rows @ since={cur_ran_at}")
        for row in items:
            pulled += 1
            rid = row.get("id")
            ran_at = row.get("ran_at")
            if rid is None or ran_at is None:
                continue  # skip malformed; never let one bad row stall the cursor
            if dry_run:
                inserted += 1  # would insert (OR IGNORE makes this an upper bound)
            else:
                # verbatim incl. cathedral_signature; rowcount = new-vs-dupe
                # (no COUNT(*) bracketing — that was O(rows) full scans)
                inserted += store.insert_row(row)
            if _tuple_gt(ran_at, rid, newest_ran_at, newest_id):
                newest_ran_at, newest_id = ran_at, rid
        # advance the page cursor to the feed's reported next position
        cur_ran_at = page.get("next_since_ran_at") or page.get("next_since") or cur_ran_at
        cur_id = page.get("next_since_id")
        # CHECKPOINT THE WATERMARK EVERY PAGE so a killed/dropped seed resumes
        # from here instead of restarting from days-ago. Idempotent (OR IGNORE
        # on rows), so an over-conservative watermark just re-pulls a page.
        if not dry_run and newest_ran_at:
            store.set_seed_state(WATERMARK_RAN_AT, newest_ran_at)
            if newest_id:
                store.set_seed_state(WATERMARK_ID, newest_id)
        if len(items) < page_limit:
            break
        if pace_secs:
            time.sleep(pace_secs)

    if not dry_run and newest_ran_at:
        store.set_seed_state(WATERMARK_RAN_AT, newest_ran_at)
        if newest_id:
            store.set_seed_state(WATERMARK_ID, newest_id)

    return {
        "pages": pages, "rows_pulled": pulled, "rows_inserted": inserted,
        "watermark_ran_at": newest_ran_at, "watermark_id": newest_id,
    }


def seed_board(
    store: Store,
    *,
    base_url: str,
    dry_run: bool,
    log=print,
) -> dict[str, Any]:
    """Mirror the live active board as metadata-only challenges (cnf_source=
    'external'). CNF bodies are not copied; cnf_url records the live fetch path.
    Idempotent: an existing challenge_id is updated in place, never duplicated."""
    board = _http_json(f"{base_url}/v1/synthetic-boolean/active-challenges")
    items = board.get("items", [])
    mirrored = skipped = 0
    by_tier: dict[int, int] = {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"

    for c in items:
        cid = c.get("challenge_id")
        tier = int(c.get("tier", 0))
        kind = c.get("kind")
        if not cid:
            skipped += 1
            continue
        by_tier[tier] = by_tier.get(tier, 0) + 1
        cnf_url = c.get("active_cnf_path") or (
            f"/api/cathedral/v1/synthetic-boolean/active-cnf?challenge_id={cid}")
        if dry_run:
            mirrored += 1
            continue

        def _do(conn, c=c, cid=cid, tier=tier, cnf_url=cnf_url):
            conn.execute(
                "INSERT INTO lane_challenges(challenge_id, family_id, tier, cnf_text, "
                "cnf_sha256, cnf_bytes, num_vars, num_clauses, status, score_multiplier, "
                "difficulty_label, designated_solver_digest, created_at_iso, cnf_source, "
                "cnf_url, updated_at_iso) "
                "VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, NULL, ?, 'external', ?, ?) "
                "ON CONFLICT(challenge_id) DO UPDATE SET "
                "  status=excluded.status, cnf_sha256=excluded.cnf_sha256, "
                "  cnf_bytes=excluded.cnf_bytes, num_vars=excluded.num_vars, "
                "  num_clauses=excluded.num_clauses, score_multiplier=excluded.score_multiplier, "
                "  difficulty_label=excluded.difficulty_label, cnf_url=excluded.cnf_url, "
                "  updated_at_iso=excluded.updated_at_iso "
                "WHERE lane_challenges.cnf_source='external'",
                (cid, c.get("family_id", "synthetic_boolean_v1"), tier,
                 c.get("cnf_sha256", ""), int(c.get("cnf_bytes", 0)),
                 int(c.get("num_vars", 0)), int(c.get("num_clauses", 0)),
                 c.get("status", "active"), float(c.get("score_multiplier", 1.0)),
                 c.get("difficulty_label"), now, cnf_url, now),
            )
        store.write(_do)
        mirrored += 1

    return {"mirrored": mirrored, "skipped": skipped, "by_tier": by_tier,
            "board_count": board.get("count", len(items))}


def run_with_store(store: Store, args: argparse.Namespace, log=print) -> dict[str, Any]:
    """Seed one pass into an ALREADY-OPEN store (does not open or close it).
    Used by the in-app boot seeder so the long-lived publisher process owns the
    connection. Returns the rows + board summary."""
    base = args.base_url.rstrip("/")
    rs = seed_rows(store, base_url=base, days=args.days, page_limit=args.page_limit,
                   pace_secs=args.pace, dry_run=args.dry_run, timeout=args.timeout,
                   max_pages=args.max_pages, log=log)
    bs = seed_board(store, base_url=base, dry_run=args.dry_run, log=log)
    return {"rows": rs, "board": bs, "total_rows": store.count_rows()}


def run(args: argparse.Namespace, log=print) -> int:
    base = args.base_url.rstrip("/")
    store = Store(args.db)
    mode = "DRY-RUN (no writes)" if args.dry_run else "LIVE seed"
    log(f"seed_live — {mode} — db={args.db} base={base}")

    log("ROWS — pulling last "
        f"{args.days}d of signed rows (tuple cursor, verbatim, no re-sign)")
    rs = seed_rows(store, base_url=base, days=args.days, page_limit=args.page_limit,
                   pace_secs=args.pace, dry_run=args.dry_run, timeout=args.timeout,
                   max_pages=args.max_pages, log=log)
    log(f"  pages={rs['pages']} pulled={rs['rows_pulled']} "
        f"inserted={rs['rows_inserted']} watermark=({rs['watermark_ran_at']}, {rs['watermark_id']})")

    log("BOARD — mirroring active challenges (metadata-only, CNF external)")
    bs = seed_board(store, base_url=base, dry_run=args.dry_run, log=log)
    log(f"  mirrored={bs['mirrored']} skipped={bs['skipped']} "
        f"by_tier={bs['by_tier']} (live board count={bs['board_count']})")

    if not args.dry_run:
        log(f"  store now holds {store.count_rows()} feed rows")
    store.close()
    log("seed_live: DONE")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Seed the thin publisher from the live public surface.")
    p.add_argument("--db", default="publisher.db", help="SQLite path for the publisher store")
    p.add_argument("--base-url", default=DEFAULT_BASE, help="live publisher base URL")
    p.add_argument("--days", type=int, default=7, help="days of feed history to seed (default 7)")
    p.add_argument("--page-limit", type=int, default=500, help="feed page size (<=500)")
    p.add_argument("--pace", type=float, default=1.0,
                   help="seconds to sleep between feed pages (rate-limit courtesy)")
    p.add_argument("--timeout", type=int, default=90, help="per-request HTTP timeout secs")
    p.add_argument("--max-pages", type=int, default=100_000,
                   help="cap pages pulled (use a small value for a quick probe)")
    p.add_argument("--dry-run", action="store_true", help="print what would be seeded; no writes")
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
