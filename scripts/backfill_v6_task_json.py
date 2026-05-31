"""Backfill the 4 v6-only fields into task_json for existing schema-6 eval_run rows.

Background
----------
The publisher signs open-window solves as schema-6 eval_runs over an 18-key
payload, but ``_task_family_storage_from_signed_row`` (before the v6 fix) kept
only the v5 subset in ``task_json``.  This left four fields absent:

  * challenge_value  — float  (from lane_challenges.score_multiplier)
  * solve_rank       — int    (from lane_challenge_solves)
  * solved           — bool   (weighted_score > 0)
  * operator         — str    (miner_hotkey)

The feed serializer's new v6 branch reads them back from task_json; without
the backfill, existing ~168 v6 rows are still served with missing fields and
the validator's signature check still fails for them.

Usage
-----
    python scripts/backfill_v6_task_json.py /path/to/publisher.db [--dry-run]

    --dry-run   Print what would change without writing.
    --db        Path to the publisher SQLite database (positional or flag).

The script is idempotent: rows that already have all four fields in task_json
are skipped.  Safe to re-run.

DO NOT run against the live production database until the v6 feed fix has been
deployed and the backfill has been reviewed in a staging environment.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("db", nargs="?", help="Path to publisher.db")
    p.add_argument("--db", dest="db_flag", help="Path to publisher.db (flag form)")
    p.add_argument("--dry-run", action="store_true", help="Print changes without writing")
    return p.parse_args()


def _reconstruct_v6_fields(
    row_id: str,
    task_json: dict,
    miner_hotkey: str,
    weighted_score: float,
    solve_rank_val: int | None,
    score_multiplier_val: float | None,
) -> dict | None:
    """Return updated task_json, or None if no change needed."""
    needs_update = (
        "challenge_value" not in task_json
        or "solve_rank" not in task_json
        or "solved" not in task_json
        or "operator" not in task_json
    )
    if not needs_update:
        return None

    updated = dict(task_json)
    if "challenge_value" not in updated:
        updated["challenge_value"] = float(score_multiplier_val) if score_multiplier_val is not None else 1.0
    if "solve_rank" not in updated:
        updated["solve_rank"] = int(solve_rank_val) if solve_rank_val is not None else 0
    if "solved" not in updated:
        updated["solved"] = bool(float(weighted_score) > 0)
    if "operator" not in updated:
        updated["operator"] = str(miner_hotkey)
    return updated


def run(db_path: str, dry_run: bool) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Fetch all schema-6 eval_run rows joined against submissions (for hotkey)
        # and optionally against lane_challenge_solves + lane_challenges.
        # lane_challenges lives in the same DB on a single-DB publisher; if the
        # challenges are in a separate file, solve_rank and score_multiplier will
        # be NULL and the defaults (rank=0, challenge_value=1.0) are used.
        cur = conn.execute(
            """
            SELECT
                er.id              AS eval_run_id,
                er.task_json       AS task_json_raw,
                er.weighted_score  AS weighted_score,
                sub.miner_hotkey   AS miner_hotkey,
                lcs.solve_rank     AS solve_rank,
                lc.score_multiplier AS score_multiplier
            FROM eval_runs er
            JOIN agent_submissions sub ON sub.id = er.submission_id
            LEFT JOIN lane_challenge_solves lcs
                ON lcs.miner_hotkey = sub.miner_hotkey
                AND lcs.eval_run_id = er.id
            LEFT JOIN lane_challenges lc
                ON lc.challenge_id = (
                    SELECT json_extract(er2.task_json, '$.challenge_id')
                    FROM eval_runs er2
                    WHERE er2.id = er.id
                )
            WHERE er.eval_output_schema_version = 6
            """
        )
        rows = cur.fetchall()
        updated_count = 0
        skipped_count = 0
        for row in rows:
            eval_run_id = row["eval_run_id"]
            task_json_raw = row["task_json_raw"]
            try:
                task_json = json.loads(task_json_raw) if task_json_raw else {}
            except (json.JSONDecodeError, TypeError):
                print(f"  WARN: could not parse task_json for eval_run {eval_run_id}, skipping")
                skipped_count += 1
                continue

            updated = _reconstruct_v6_fields(
                row_id=eval_run_id,
                task_json=task_json,
                miner_hotkey=row["miner_hotkey"],
                weighted_score=row["weighted_score"],
                solve_rank_val=row["solve_rank"],
                score_multiplier_val=row["score_multiplier"],
            )
            if updated is None:
                skipped_count += 1
                continue

            new_json = json.dumps(updated, sort_keys=True)
            if dry_run:
                print(
                    f"  DRY-RUN eval_run={eval_run_id}: "
                    f"challenge_value={updated.get('challenge_value')}, "
                    f"solve_rank={updated.get('solve_rank')}, "
                    f"solved={updated.get('solved')}, "
                    f"operator={updated.get('operator')!r}"
                )
            else:
                conn.execute(
                    "UPDATE eval_runs SET task_json = ? WHERE id = ?",
                    (new_json, eval_run_id),
                )
            updated_count += 1

        if not dry_run:
            conn.commit()
        print(
            f"\nBackfill complete: {updated_count} rows updated, "
            f"{skipped_count} skipped (already complete or unparseable)."
            + (" [DRY RUN — no writes committed]" if dry_run else "")
        )
    finally:
        conn.close()


def main() -> None:
    args = _parse_args()
    db_path_str = args.db_flag or args.db
    if not db_path_str:
        print("ERROR: provide the path to publisher.db as a positional argument or --db flag.", file=sys.stderr)
        sys.exit(1)
    db_path = Path(db_path_str)
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        sys.exit(1)
    run(str(db_path), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
