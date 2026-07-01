#!/usr/bin/env python3
"""Local E2E smoke for the V2 lean bitset ingress.

Starts no server by itself. Run against a local ingress, for example:

  CATHEDRAL_V2_SUBMIT_TOKEN_SECRET=dev-secret \
  CATHEDRAL_V2_INGRESS_DB_PATH=/tmp/v2-ingress.sqlite3 \
  PYTHONPATH=. python3 -m uvicorn scaffold.publisher.v2_lean_ingress:app \
    --host 127.0.0.1 --port 8799

Then:

  PYTHONPATH=. python3 scripts/v2_lean_ingress_e2e.py \
    --base http://127.0.0.1:8799 --secret dev-secret
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bittensor_wallet import Keypair

from scaffold.publisher import v2_bitset_submit, v2_pipeline


def _iso_now() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _iso_plus(secs: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=secs)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _http_json(method: str, url: str, *, body: dict | None = None, headers: dict[str, str] | None = None):
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib_request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib_request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw)
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"text": raw}
        return exc.code, payload
    except URLError as exc:
        raise SystemExit(f"request failed: {exc}") from exc


def build_submit(secret: str, key_uri: str):
    kp = Keypair.create_from_uri(key_uri)
    submitted_at = _iso_now()
    expires_at = _iso_plus(300)
    challenge_id = "pm-t2-e495232-s7-lean-e2e"
    epoch = 495232
    tier = 2
    seq = 7
    nvars = 10
    cnf_sha256 = hashlib.sha256(b"lean-ingress-e2e-cnf").hexdigest()
    assignment = [1, -2, 3, -4, 5, -6, 7, -8, 9, -10]
    assignment_raw = v2_pipeline.encode_bitset_assignment(assignment)
    assignment_b64 = base64.b64encode(assignment_raw).decode("ascii")
    token = v2_bitset_submit.mint_submit_token(
        secret=secret,
        miner_hotkey=kp.ss58_address,
        challenge_id=challenge_id,
        epoch=epoch,
        tier=tier,
        seq=seq,
        nvars=nvars,
        cnf_sha256=cnf_sha256,
        expires_at=expires_at,
    )
    body = {
        "schema": v2_bitset_submit.SCHEMA,
        "card_id": "synthetic_boolean_v1",
        "miner_hotkey": kp.ss58_address,
        "submitted_at": submitted_at,
        "challenge_id": challenge_id,
        "submit_token": token,
        "assignment_encoding": v2_bitset_submit.ASSIGNMENT_ENCODING,
        "assignment_b64": assignment_b64,
    }
    submit = v2_bitset_submit.normalize_submit_body(
        body,
        miner_hotkey=kp.ss58_address,
        submitted_at=submitted_at,
        card_id="synthetic_boolean_v1",
    )
    sig = base64.b64encode(kp.sign(v2_bitset_submit.canonical_submit_bytes(submit))).decode("ascii")
    headers = {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sig,
        "X-Cathedral-Submitted-At": submitted_at,
    }
    return body, headers


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8799")
    parser.add_argument("--secret", default="dev-v2-lean-ingress-secret-not-live")
    parser.add_argument("--key-uri", default="//V2LeanIngressE2E")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    status, health = _http_json("GET", f"{base}/health/live")
    if status != 200 or health.get("status") != "ok":
        raise SystemExit(f"health failed: {status} {health}")

    body, headers = build_submit(args.secret, args.key_uri)
    status, receipt = _http_json("POST", f"{base}/v2/agents/submit-bitset", body=body, headers=headers)
    if status != 202 or receipt.get("status") != "received" or receipt.get("terminal") is not False:
        raise SystemExit(f"first submit failed: {status} {receipt}")
    rid = receipt["receipt_id"]

    status2, replay = _http_json("POST", f"{base}/v2/agents/submit-bitset", body=body, headers=headers)
    if status2 != 200 or replay.get("receipt_id") != rid or replay.get("idempotent_replay") is not True:
        raise SystemExit(f"duplicate submit failed: {status2} {replay}")

    status3, fetched = _http_json("GET", f"{base}/v2/agents/submit-bitset/receipts/{rid}")
    if status3 != 200 or fetched.get("receipt_id") != rid:
        raise SystemExit(f"receipt fetch failed: {status3} {fetched}")

    status4, metrics = _http_json("GET", f"{base}/v2/ingress/metrics")
    if status4 != 200 or metrics.get("events", {}).get("received", 0) < 1:
        raise SystemExit(f"metrics failed: {status4} {metrics}")

    print("E2E_OK")
    print(f"receipt_id={rid}")
    print(f"status={receipt['status']}")
    print(f"idempotent_replay={replay['idempotent_replay']}")
    print(f"unflushed_events={metrics.get('unflushed_events')}")


if __name__ == "__main__":
    main()
