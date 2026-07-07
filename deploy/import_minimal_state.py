#!/usr/bin/env python3
"""Load minimal Cathedral live-scoring CSV gzip export into a fresh Postgres DB."""
from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path

import psycopg2

JOBS = [
    ("signed_weight_vectors", "signed_weight_vectors_latest.csv.gz"),
    ("weight_policy_state", "weight_policy_state.csv.gz"),
    ("seed_state", "seed_state.csv.gz"),
    ("metagraph_hotkeys", "metagraph_hotkeys.csv.gz"),
    ("coldkey_map", "coldkey_map.csv.gz"),
    ("external_score_reports", "external_score_reports.csv.gz"),
    ("external_score_entries", "external_score_entries.csv.gz"),
    ("per_miner_solves", "per_miner_solves_48h.csv.gz"),
]


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: import_minimal_state.py DATABASE_URL STATE_DIR", file=sys.stderr)
        return 2
    dsn = sys.argv[1]
    state_dir = Path(sys.argv[2]).expanduser().resolve()
    manifest = json.loads((state_dir / "manifest.json").read_text())
    print("manifest", json.dumps({
        "exported_at": manifest.get("exported_at"),
        "per_miner_cutoff": manifest.get("per_miner_cutoff"),
        "external_cutoff": manifest.get("external_cutoff"),
    }, sort_keys=True))

    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '0'")
    cur.execute("SET synchronous_commit = off")
    for table, filename in JOBS:
        path = state_dir / filename
        if not path.exists():
            raise FileNotFoundError(path)
        print(f"[import] truncating {table}", flush=True)
        cur.execute(f"TRUNCATE TABLE {table}")
        print(f"[import] loading {table} from {filename}", flush=True)
        with gzip.open(path, "rt", newline="") as fh:
            cur.copy_expert(f"COPY {table} FROM STDIN WITH CSV HEADER", fh)
        cur.execute(f"SELECT count(*) FROM {table}")
        print(f"[import] {table} rows={cur.fetchone()[0]}", flush=True)
        conn.commit()
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
