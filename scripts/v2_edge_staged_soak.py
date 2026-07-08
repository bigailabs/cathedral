#!/usr/bin/env python3
"""Soak the staged V2 edge gate without opening miner access.

This sends many signed, non-canary V2 miner read requests through the public edge
and requires Cloudflare to reject them locally as `v2_beta_staged_reopen`.
It is intentionally a no-origin-load proof for the current closed-gate posture.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

try:
    import requests
except Exception as exc:  # pragma: no cover
    raise SystemExit("missing dependency: pip install requests") from exc


DEFAULT_BASE = "https://v2-beta.cathedral.computer"
EXPECTED_REASON = "v2_beta_staged_reopen"
EXPECTED_ORIGIN = "edge-staged-reopen"


def _load_e2e_module():
    path = Path(__file__).with_name("v2_bitset_miner_e2e.py")
    spec = importlib.util.spec_from_file_location("v2_bitset_miner_e2e", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


e2e = _load_e2e_module()


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    if lo == hi:
        return float(ordered[lo])
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo))


def _json_response(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return resp.text


def request_one(base: str, uri: str, idx: int, *, timeout: float) -> dict[str, Any]:
    kp = e2e.Keypair.create_from_uri(uri)
    session = requests.Session()
    url = base.rstrip("/") + "/v2/synthetic-boolean/per-miner/challenges?limit=10"
    started = time.time()
    resp = session.get(url, headers=e2e.read_headers(kp), timeout=timeout)
    elapsed_ms = (time.time() - started) * 1000.0
    body = _json_response(resp)
    reason = ""
    if isinstance(body, dict):
        reason = str(body.get("reason") or body.get("detail") or "")
    return {
        "idx": idx,
        "uri": uri,
        "hotkey": kp.ss58_address,
        "status": resp.status_code,
        "elapsed_ms": elapsed_ms,
        "reason": reason,
        "retry_after": resp.headers.get("retry-after"),
        "edge_origin": resp.headers.get("x-cathedral-v2-beta-origin"),
        "router": resp.headers.get("x-cathedral-v2-beta-router"),
        "rejection_reason": resp.headers.get("x-cathedral-rejection-reason"),
        "body_sample": body if isinstance(body, dict) else str(body)[:200],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--requests", type=int, default=64)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--max-p95-ms", type=float, default=1500.0)
    parser.add_argument("--uri-prefix", default="//EdgeStagedSoak")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    total = max(1, int(args.requests))
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    uris = [f"{args.uri_prefix}//{stamp}//{idx}" for idx in range(total)]
    print(
        "edge_staged_soak "
        f"base={base} requests={total} concurrency={args.concurrency}"
    )

    started = time.time()
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, int(args.concurrency))) as pool:
        futures = {
            pool.submit(request_one, base, uri, idx, timeout=args.timeout): idx
            for idx, uri in enumerate(uris)
        }
        for fut in as_completed(futures):
            try:
                row = fut.result()
            except Exception as exc:
                errors.append(f"request_error idx={futures[fut]} error={exc!r}")
                continue
            rows.append(row)
            if row["status"] != 429:
                errors.append(f"idx={row['idx']} status={row['status']} expected=429")
            if row["reason"] != EXPECTED_REASON:
                errors.append(f"idx={row['idx']} reason={row['reason']!r}")
            if row["rejection_reason"] != EXPECTED_REASON:
                errors.append(
                    f"idx={row['idx']} rejection_header={row['rejection_reason']!r}"
                )
            if row["edge_origin"] != EXPECTED_ORIGIN:
                errors.append(f"idx={row['idx']} edge_origin={row['edge_origin']!r}")
            if row["router"] != "cloudflare-worker":
                errors.append(f"idx={row['idx']} router={row['router']!r}")

    elapsed_ms = [float(r["elapsed_ms"]) for r in rows]
    status_counts: dict[str, int] = {}
    origin_counts: dict[str, int] = {}
    for row in rows:
        status_counts[str(row["status"])] = status_counts.get(str(row["status"]), 0) + 1
        origin = str(row.get("edge_origin") or "")
        origin_counts[origin] = origin_counts.get(origin, 0) + 1

    summary = {
        "schema": "cathedral.v2.edge_staged_soak.v1",
        "base": base,
        "requests": total,
        "completed": len(rows),
        "concurrency": int(args.concurrency),
        "elapsed_secs": round(time.time() - started, 3),
        "status_counts": status_counts,
        "origin_counts": origin_counts,
        "latency_ms_p50": round(statistics.median(elapsed_ms), 1) if elapsed_ms else 0,
        "latency_ms_p95": round(_percentile(elapsed_ms, 0.95), 1),
        "latency_ms_max": round(max(elapsed_ms), 1) if elapsed_ms else 0,
        "errors": errors[:10],
    }
    if len(rows) != total:
        errors.append(f"completed={len(rows)}/{total}")
    if summary["latency_ms_p95"] > float(args.max_p95_ms):
        errors.append(f"latency_p95_ms={summary['latency_ms_p95']} > {args.max_p95_ms}")

    print("edge_staged_summary " + json.dumps(summary, sort_keys=True))
    if errors:
        print("EDGE_STAGED_FAILED " + "; ".join(errors[:20]))
        return 1
    print("EDGE_STAGED_OK " + json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
