#!/usr/bin/env python3
"""Stream minimal Cathedral live-scoring state out of the large Railway Postgres.

Writes gzip CSV files locally. Does not create temp tables or write to DB server disk.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2


def iso_ms(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def copy_query(cur, name: str, query: str, out_dir: Path) -> dict:
    path = out_dir / f"{name}.csv.gz"
    sql = f"COPY ({query}) TO STDOUT WITH CSV HEADER"
    with gzip.open(path, "wt", newline="") as fh:
        cur.copy_expert(sql, fh)
    # Count rows from gzip CSV locally, not on DB.
    with gzip.open(path, "rt", newline="") as fh:
        reader = csv.reader(fh)
        rows = max(0, sum(1 for _ in reader) - 1)
    return {"name": name, "path": str(path), "rows": rows, "bytes": path.stat().st_size, "query": query}


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: export_minimal_state.py DATABASE_URL OUT_DIR", file=sys.stderr)
        return 2
    db_url = sys.argv[1]
    out_dir = Path(sys.argv[2]).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)

    now = datetime.now(timezone.utc)
    per_miner_cutoff = iso_ms(now - timedelta(hours=48))
    external_cutoff = iso_ms(now - timedelta(hours=24))

    conn = psycopg2.connect(db_url)
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '120s'")

    jobs = [
        ("signed_weight_vectors_latest", "SELECT * FROM signed_weight_vectors WHERE id = 'latest'"),
        ("weight_policy_state", "SELECT * FROM weight_policy_state"),
        ("seed_state", "SELECT * FROM seed_state"),
        ("metagraph_hotkeys", "SELECT * FROM metagraph_hotkeys"),
        ("coldkey_map", "SELECT * FROM coldkey_map"),
        ("external_score_reports", f"SELECT * FROM external_score_reports WHERE received_at_iso > '{external_cutoff}'"),
        ("external_score_entries", f"SELECT * FROM external_score_entries WHERE received_at_iso > '{external_cutoff}'"),
        ("per_miner_solves_48h", f"SELECT * FROM per_miner_solves WHERE solved_at_iso > '{per_miner_cutoff}' AND verified = 1"),
    ]

    manifest = {
        "exported_at": iso_ms(now),
        "per_miner_cutoff": per_miner_cutoff,
        "external_cutoff": external_cutoff,
        "jobs": [],
    }
    for name, query in jobs:
        print(f"[export] {name}", flush=True)
        try:
            manifest["jobs"].append(copy_query(cur, name, query, out_dir))
        except Exception as exc:
            manifest["jobs"].append({"name": name, "error": repr(exc), "query": query})
            print(f"[export] {name} ERROR {exc!r}", file=sys.stderr, flush=True)

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    os.chmod(manifest_path, 0o600)
    for p in out_dir.glob("*.csv.gz"):
        os.chmod(p, 0o600)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
