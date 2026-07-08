#!/usr/bin/env python3
"""Capacity probe for Cathedral V2 bitset submit + async verify drain.

This intentionally drives the real miner wire path:
  challenge page -> CNF fetch/token -> local solve -> concurrent bitset submit
  -> receipt polling -> verify metrics.

Use direct sandbox tunnels while the public edge gate is staged, e.g.:
  ssh -N -L 18080:127.0.0.1:8000 -L 18081:127.0.0.1:8080 polaris@34.71.88.140
  python scripts/v2_bitset_capacity_probe.py \
    --challenge-base http://127.0.0.1:18081 \
    --submit-base http://127.0.0.1:18081 \
    --metrics-base http://127.0.0.1:18080
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

try:
    import requests
except Exception as exc:  # pragma: no cover
    raise SystemExit("missing dependency: pip install requests") from exc


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


@dataclass
class PreparedSubmit:
    uri: str
    hotkey: str
    challenge_id: str
    tier: int
    seq: int
    epoch: int
    body: dict[str, Any]
    keypair: Any
    solve_secs: float
    cnf_bytes: int


def _json_response(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return resp.text


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


def _uris(args: argparse.Namespace) -> list[str]:
    out: list[str] = []
    for uri in args.uri or []:
        if uri.strip():
            out.append(uri.strip())
    for part in (args.uris or "").replace("\n", ",").split(","):
        if part.strip():
            out.append(part.strip())
    if out:
        return out
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    prefix = args.uri_prefix.strip() or "//CapacityProbe"
    return [f"{prefix}//{stamp}//{idx}" for idx in range(max(1, int(args.miners)))]


def fetch_metrics(base: str, *, timeout: float) -> dict[str, Any] | None:
    try:
        resp = requests.get(base.rstrip("/") + "/v2/verify/metrics", timeout=timeout)
        if resp.status_code != 200:
            return None
        payload = resp.json()
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def prepare_for_uri(uri: str, args: argparse.Namespace) -> list[PreparedSubmit]:
    session = requests.Session()
    challenge_base = (args.challenge_base or args.base).rstrip("/")
    kp = e2e.Keypair.create_from_uri(uri)
    r = session.get(challenge_base + "/health/live", timeout=args.http_timeout)
    if r.status_code != 200:
        raise RuntimeError(f"{uri}: health status {r.status_code}: {r.text[:300]}")

    path = f"/v2/synthetic-boolean/per-miner/challenges?limit={int(args.per_miner_limit)}"
    r, payload = e2e.get_json(
        session,
        challenge_base,
        path,
        headers=e2e.read_headers(kp),
        timeout=args.http_timeout,
    )
    if r.status_code != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"{uri}: challenges status {r.status_code}: {str(payload)[:500]}")
    if payload.get("submit_path") != "/v2/agents/submit-bitset":
        raise RuntimeError(f"{uri}: bitset submit disabled: {payload.get('submit_path')}")
    items = list(payload.get("items") or [])[: max(1, int(args.per_miner_limit))]
    if not items:
        raise RuntimeError(f"{uri}: no challenge items")

    prepared: list[PreparedSubmit] = []
    for item in items:
        challenge_id = str(item["challenge_id"])
        tier = int(item.get("tier") or 1)
        seq = int(item.get("seq") or 0)
        epoch = int(item.get("epoch") or payload.get("epoch") or 0)
        submit_token = str(item.get("submit_token") or "")
        query = urlencode({"challenge_id": challenge_id, "tier": tier, "seq": seq})
        r = session.get(
            challenge_base + "/v2/synthetic-boolean/per-miner/cnf?" + query,
            headers=e2e.read_headers(kp),
            timeout=args.http_timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(f"{uri}: cnf {challenge_id} status {r.status_code}: {r.text[:300]}")
        cnf_text = r.text
        if not submit_token:
            submit_token = str(r.headers.get("X-Cathedral-Submit-Token") or "")
        if not submit_token:
            raise RuntimeError(f"{uri}: missing submit token for {challenge_id}")

        t0 = time.time()
        assignment, solver_used = e2e.solve_assignment(cnf_text, args.solver)
        solve_secs = time.time() - t0
        assignment_b64 = e2e.base64.b64encode(
            e2e.encode_bitset_assignment(assignment)
        ).decode("ascii")
        body = {
            "schema": e2e.BITSET_SCHEMA,
            "card_id": e2e.CARD_ID,
            "challenge_id": challenge_id,
            "submit_token": submit_token,
            "assignment_encoding": "bitset/v1",
            "assignment_b64": assignment_b64,
            "solver_id": solver_used,
            "solver_hash": e2e.solver_hash_for(solver_used),
        }
        prepared.append(
            PreparedSubmit(
                uri=uri,
                hotkey=kp.ss58_address,
                challenge_id=challenge_id,
                tier=tier,
                seq=seq,
                epoch=epoch,
                body=body,
                keypair=kp,
                solve_secs=solve_secs,
                cnf_bytes=len(cnf_text.encode("utf-8")),
            )
        )
    return prepared


def submit_one(base: str, item: PreparedSubmit, *, timeout: float) -> dict[str, Any]:
    session = requests.Session()
    t0 = time.time()
    resp = session.post(
        base.rstrip("/") + "/v2/agents/submit-bitset",
        json=item.body,
        headers=e2e.bitset_headers(item.keypair, item.body),
        timeout=timeout,
    )
    admit_ms = (time.time() - t0) * 1000.0
    payload = _json_response(resp)
    if resp.status_code not in (200, 202) or not isinstance(payload, dict):
        raise RuntimeError(
            f"submit {item.hotkey} {item.challenge_id} status={resp.status_code} "
            f"body={str(payload)[:500]}"
        )
    return {
        "status_code": resp.status_code,
        "admit_ms": admit_ms,
        "receipt_id": payload.get("receipt_id"),
        "receipt_status": payload.get("status"),
        "idempotent_replay": bool(payload.get("idempotent_replay")),
        "hotkey": item.hotkey,
        "challenge_id": item.challenge_id,
    }


def poll_receipts(
    base: str,
    receipts: list[dict[str, Any]],
    *,
    deadline: float,
    interval_secs: float,
    timeout: float,
    metrics_base: str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    session = requests.Session()
    metrics_url_base = (metrics_base or base).rstrip("/")
    remaining = {str(r["receipt_id"]): r for r in receipts if r.get("receipt_id")}
    finals: dict[str, dict[str, Any]] = {}
    metric_samples: list[dict[str, Any]] = []
    while remaining and time.time() < deadline:
        metrics = fetch_metrics(metrics_url_base, timeout=timeout)
        if metrics:
            metric_samples.append(metrics)
        for receipt_id in list(remaining):
            resp = session.get(
                base.rstrip() + f"/v2/agents/submit-bitset/receipts/{receipt_id}",
                timeout=timeout,
            )
            payload = _json_response(resp)
            if resp.status_code == 200 and isinstance(payload, dict) and payload.get("terminal"):
                finals[receipt_id] = payload
                remaining.pop(receipt_id, None)
        if remaining:
            time.sleep(max(0.1, interval_secs))
    return finals, metric_samples


def settle_metrics(base: str, *, timeout: float, settle_secs: float, interval_secs: float) -> dict[str, Any] | None:
    deadline = time.time() + max(0.0, settle_secs)
    last = fetch_metrics(base, timeout=timeout)
    while last and int(last.get("pending_count") or 0) != 0 and time.time() < deadline:
        time.sleep(max(0.1, interval_secs))
        last = fetch_metrics(base, timeout=timeout) or last
    return last


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=e2e.DEFAULT_BASE)
    parser.add_argument("--challenge-base", default="")
    parser.add_argument("--submit-base", default="")
    parser.add_argument("--metrics-base", default="")
    parser.add_argument("--uri", action="append", default=[], help="Miner URI; repeatable")
    parser.add_argument("--uris", default="", help="Comma-separated miner URIs")
    parser.add_argument("--uri-prefix", default="//CapacityProbe")
    parser.add_argument("--miners", type=int, default=4)
    parser.add_argument("--per-miner-limit", type=int, default=4)
    parser.add_argument("--prepare-concurrency", type=int, default=4)
    parser.add_argument("--submit-concurrency", type=int, default=8)
    parser.add_argument("--solver", default="cadical153")
    parser.add_argument("--http-timeout", type=float, default=30.0)
    parser.add_argument("--poll-interval-secs", type=float, default=0.5)
    parser.add_argument("--max-drain-secs", type=float, default=20.0)
    parser.add_argument("--max-admit-p95-ms", type=float, default=1000.0)
    parser.add_argument(
        "--metrics-settle-secs",
        type=float,
        default=25.0,
        help="Wait for /v2/verify/metrics cache to refresh after receipts drain",
    )
    parser.add_argument("--min-verified-ratio", type=float, default=1.0)
    parser.add_argument("--max-rejected", type=int, default=0)
    parser.add_argument("--allow-replays", action="store_true")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    challenge_base = (args.challenge_base or base).rstrip("/")
    submit_base = (args.submit_base or base).rstrip("/")
    metrics_base = (args.metrics_base or submit_base).rstrip("/")
    uris = _uris(args)
    expected = len(uris) * max(1, int(args.per_miner_limit))
    print(
        "capacity_probe "
        f"challenge_base={challenge_base} submit_base={submit_base} "
        f"metrics_base={metrics_base} miners={len(uris)} per_miner_limit={args.per_miner_limit} "
        f"expected_submits={expected}"
    )

    if submit_base != challenge_base:
        try:
            r = requests.get(submit_base + "/health/live", timeout=args.http_timeout)
            if r.status_code != 200:
                print(f"CAPACITY_FAILED submit_health status={r.status_code} body={r.text[:300]}")
                return 1
        except Exception as exc:
            print(f"CAPACITY_FAILED submit_health error={exc!r}")
            return 1

    before = fetch_metrics(metrics_base, timeout=args.http_timeout)
    if before:
        print(
            "metrics_before "
            f"enabled={before.get('enabled')} pending={before.get('pending_count')} "
            f"processed_last_60s={before.get('processed_last_60s')} "
            f"tick_errors={before.get('tick_errors_last_60s')}"
        )
        if before.get("enabled") is not True:
            print("CAPACITY_FAILED verifier_metrics_not_enabled")
            return 1

    prepared: list[PreparedSubmit] = []
    prep_started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, int(args.prepare_concurrency))) as pool:
        futures = {pool.submit(prepare_for_uri, uri, args): uri for uri in uris}
        for fut in as_completed(futures):
            uri = futures[fut]
            rows = fut.result()
            prepared.extend(rows)
            print(f"prepared uri={uri} hotkey={rows[0].hotkey} count={len(rows)}")
    prep_secs = time.time() - prep_started
    if len(prepared) != expected:
        print(f"CAPACITY_FAILED prepared={len(prepared)} expected={expected}")
        return 1

    solve_secs = [p.solve_secs for p in prepared]
    print(
        "prepared_summary "
        f"count={len(prepared)} prep_secs={prep_secs:.3f} "
        f"solve_p50={statistics.median(solve_secs):.3f}s "
        f"solve_p95={_percentile(solve_secs, 0.95):.3f}s"
    )

    receipts: list[dict[str, Any]] = []
    submit_started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, int(args.submit_concurrency))) as pool:
        futures = {pool.submit(submit_one, submit_base, item, timeout=args.http_timeout): item for item in prepared}
        for fut in as_completed(futures):
            result = fut.result()
            receipts.append(result)
            print(
                "submitted "
                f"status={result['status_code']} receipt={result['receipt_id']} "
                f"admit_ms={result['admit_ms']:.1f} replay={result['idempotent_replay']} "
                f"hotkey={result['hotkey']} challenge={result['challenge_id']}"
            )
    submit_finished = time.time()

    if not args.allow_replays and any(r.get("idempotent_replay") for r in receipts):
        print("CAPACITY_FAILED unexpected_idempotent_replay")
        return 1

    deadline = submit_finished + max(1.0, float(args.max_drain_secs))
    finals, samples = poll_receipts(
        submit_base,
        receipts,
        deadline=deadline,
        interval_secs=args.poll_interval_secs,
        timeout=args.http_timeout,
        metrics_base=metrics_base,
    )
    drained_at = time.time()
    verified = [r for r in finals.values() if r.get("status") == "verified"]
    rejected = [r for r in finals.values() if r.get("status") == "rejected"]
    terminal = len(finals)
    admit_values = [float(r["admit_ms"]) for r in receipts]
    max_pending = max([int(s.get("pending_count") or 0) for s in samples] or [0])
    after = settle_metrics(
        metrics_base,
        timeout=args.http_timeout,
        settle_secs=args.metrics_settle_secs,
        interval_secs=args.poll_interval_secs,
    )
    if after:
        max_pending = max(max_pending, int(after.get("pending_count") or 0))

    summary = {
        "schema": "cathedral.v2.bitset_capacity_probe.v1",
        "base": base,
        "challenge_base": challenge_base,
        "submit_base": submit_base,
        "metrics_base": metrics_base,
        "miners": len(uris),
        "per_miner_limit": int(args.per_miner_limit),
        "submitted": len(receipts),
        "terminal": terminal,
        "verified": len(verified),
        "rejected": len(rejected),
        "submit_window_secs": round(submit_finished - submit_started, 3),
        "drain_secs_after_submit": round(drained_at - submit_finished, 3),
        "total_submit_to_drain_secs": round(drained_at - submit_started, 3),
        "admit_ms_p50": round(statistics.median(admit_values), 1) if admit_values else 0,
        "admit_ms_p95": round(_percentile(admit_values, 0.95), 1),
        "admit_ms_max": round(max(admit_values), 1) if admit_values else 0,
        "max_observed_pending": max_pending,
        "metrics_after": {
            "enabled": after.get("enabled") if after else None,
            "pending_count": after.get("pending_count") if after else None,
            "processed_last_60s": after.get("processed_last_60s") if after else None,
            "last_batch_count": after.get("last_batch_count") if after else None,
            "last_batch_ms": after.get("last_batch_ms") if after else None,
            "tick_errors_last_60s": after.get("tick_errors_last_60s") if after else None,
            "lock_held_by_self": after.get("lock_held_by_self") if after else None,
        },
    }

    print("capacity_summary " + json.dumps(summary, sort_keys=True))
    verified_ratio = (len(verified) / len(receipts)) if receipts else 0.0
    failures = []
    if terminal != len(receipts):
        failures.append(f"terminal={terminal}/{len(receipts)}")
    if verified_ratio < float(args.min_verified_ratio):
        failures.append(f"verified_ratio={verified_ratio:.3f} < {args.min_verified_ratio}")
    if len(rejected) > int(args.max_rejected):
        failures.append(f"rejected={len(rejected)} > {args.max_rejected}")
    if summary["admit_ms_p95"] > float(args.max_admit_p95_ms):
        failures.append(f"admit_p95_ms={summary['admit_ms_p95']} > {args.max_admit_p95_ms}")
    if after and int(after.get("pending_count") or 0) != 0:
        failures.append(f"pending_after={after.get('pending_count')}")
    if after and after.get("lock_held_by_self") is not True:
        failures.append("verifier_lock_not_held")
    if after and int(after.get("tick_errors_last_60s") or 0) != 0:
        failures.append(f"tick_errors={after.get('tick_errors_last_60s')}")

    if failures:
        print("CAPACITY_FAILED " + "; ".join(failures))
        return 1

    print("CAPACITY_OK " + json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
