#!/usr/bin/env python3
"""Cathedral V2 bitset miner E2E smoke.

Flow:
  fetch V2 per-miner challenge + submit_token -> fetch CNF -> solve locally ->
  submit tiny signed bitset -> check receipt -> check V2 shadow weights.

This script writes only to isolated V2 beta tables. It does not affect V1
production weights, rewards, or payouts.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

try:
    import requests
except Exception as exc:  # pragma: no cover
    raise SystemExit("missing dependency: pip install requests") from exc

try:
    from bittensor_wallet import Keypair
except Exception as exc:  # pragma: no cover
    raise SystemExit("missing dependency: pip install bittensor-wallet") from exc

CARD_ID = "synthetic_boolean_v1"
BITSET_SCHEMA = "cathedral.v2.submit_bitset.v1"
EMPTY_BUNDLE_HASH = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
DEFAULT_BASE = "https://v2-beta.cathedral.computer"
FALLBACK_SOLVERS = ("cadical153", "glucose3", "minisat22")


def now_iso() -> str:
    d = datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def canonical_claim_bytes(*, miner_hotkey: str, submitted_at: str) -> bytes:
    return canonical_json_bytes({
        "bundle_hash": EMPTY_BUNDLE_HASH,
        "card_id": CARD_ID,
        "miner_hotkey": miner_hotkey,
        "submitted_at": submitted_at,
        "challenge_id": "",
        "dimacs_solution_sha256": "",
    })


def canonical_bitset_submit_bytes(body: dict[str, Any], *, miner_hotkey: str, submitted_at: str) -> bytes:
    submit = {
        "schema": BITSET_SCHEMA,
        "card_id": CARD_ID,
        "miner_hotkey": miner_hotkey,
        "submitted_at": submitted_at,
        "challenge_id": str(body["challenge_id"]).strip(),
        "submit_token": str(body["submit_token"]).strip(),
        "assignment_encoding": "bitset/v1",
        "assignment_b64": str(body["assignment_b64"]).strip(),
    }
    return canonical_json_bytes({k: submit[k] for k in sorted(submit)})


def sign_b64(kp: Keypair, msg: bytes) -> str:
    return base64.b64encode(kp.sign(msg)).decode("ascii")


def read_headers(kp: Keypair) -> dict[str, str]:
    ts = now_iso()
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sign_b64(kp, canonical_claim_bytes(miner_hotkey=kp.ss58_address, submitted_at=ts)),
        "X-Cathedral-Submitted-At": ts,
    }


def bitset_headers(kp: Keypair, body: dict[str, Any]) -> dict[str, str]:
    ts = now_iso()
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sign_b64(kp, canonical_bitset_submit_bytes(body, miner_hotkey=kp.ss58_address, submitted_at=ts)),
        "X-Cathedral-Submitted-At": ts,
    }


def make_keypair(args: argparse.Namespace) -> tuple[Keypair, str]:
    uri = os.environ.get("CATHEDRAL_MINER_URI", "").strip()
    seed = os.environ.get("CATHEDRAL_MINER_SEED_HEX", "").strip()
    if uri:
        return Keypair.create_from_uri(uri), "env:CATHEDRAL_MINER_URI"
    if seed:
        if seed.startswith("0x"):
            seed = seed[2:]
        return Keypair.create_from_seed("0x" + seed), "env:CATHEDRAL_MINER_SEED_HEX"
    if args.uri:
        return Keypair.create_from_uri(args.uri), "arg:uri"
    return Keypair.create_from_seed("0x" + os.urandom(32).hex()), "ephemeral"


def parse_dimacs(cnf_text: str) -> tuple[int, list[list[int]]]:
    clauses: list[list[int]] = []
    nvars = 0
    for raw in cnf_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("c"):
            continue
        if line.startswith("p "):
            parts = line.split()
            if len(parts) >= 4 and parts[1] == "cnf":
                nvars = int(parts[2])
            continue
        lits = [int(tok) for tok in line.split() if tok != "0"]
        if lits:
            clauses.append(lits)
            nvars = max(nvars, *(abs(x) for x in lits))
    return nvars, clauses


def solve_assignment(cnf_text: str, preferred_solver: str) -> tuple[list[int], str]:
    try:
        from pysat.formula import CNF
        from pysat.solvers import Solver
    except Exception as exc:  # pragma: no cover
        raise SystemExit("missing dependency: pip install python-sat") from exc

    nvars, clauses = parse_dimacs(cnf_text)
    cnf = CNF()
    cnf.nv = nvars
    for clause in clauses:
        cnf.append(clause)
    solvers = [preferred_solver] if preferred_solver else []
    for name in FALLBACK_SOLVERS:
        if name not in solvers:
            solvers.append(name)
    last_err: Exception | None = None
    for solver_name in solvers:
        try:
            with Solver(name=solver_name, bootstrap_with=cnf.clauses) as solver:
                if not solver.solve():
                    raise RuntimeError("solver returned UNSAT for assigned challenge")
                model = solver.get_model() or []
            vals: dict[int, bool] = {}
            for lit in model:
                v = abs(int(lit))
                if 1 <= v <= nvars and v not in vals:
                    vals[v] = lit > 0
            return [i if vals.get(i, False) else -i for i in range(1, nvars + 1)], solver_name
        except Exception as exc:
            last_err = exc
            continue
    raise RuntimeError(f"no SAT solver succeeded; last_error={last_err}")


def encode_bitset_assignment(assignment: list[int]) -> bytes:
    nvars = len(assignment)
    out = bytearray((nvars + 7) // 8)
    seen: set[int] = set()
    for lit in assignment:
        lit_i = int(lit)
        v = abs(lit_i)
        if v < 1 or v > nvars or v in seen:
            raise ValueError("invalid_assignment")
        seen.add(v)
        if lit_i > 0:
            out[(v - 1) // 8] |= 1 << ((v - 1) % 8)
    if len(seen) != nvars:
        raise ValueError("incomplete_assignment")
    return bytes(out)


def get_json(session: requests.Session, base: str, path: str, *, headers: dict[str, str] | None = None, timeout: float = 30.0) -> tuple[requests.Response, Any]:
    r = session.get(base.rstrip("/") + path, headers=headers or {}, timeout=timeout)
    try:
        return r, r.json()
    except Exception:
        return r, r.text


def main() -> int:
    ap = argparse.ArgumentParser(description="Run an isolated Cathedral V2 bitset miner E2E smoke")
    ap.add_argument("--base", default=os.environ.get("CATHEDRAL_V2_BASE_URL", DEFAULT_BASE).rstrip("/"), help="Default base for both challenge and submit paths")
    ap.add_argument("--challenge-base", default=os.environ.get("CATHEDRAL_V2_CHALLENGE_BASE_URL", ""), help="Fetch V2 challenges/CNFs from this base; defaults to --base")
    ap.add_argument("--submit-base", default=os.environ.get("CATHEDRAL_V2_SUBMIT_BASE_URL", ""), help="Submit V2 bitsets to this base; defaults to --base")
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--solver", default=os.environ.get("CATHEDRAL_MINER_SOLVER", "cadical153"))
    ap.add_argument("--poll-secs", type=float, default=30.0)
    ap.add_argument("--uri", default="", help="Optional dev URI, e.g. //Alice")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--repeat-submit", type=int, default=1, help="Repeat the same solved submit N times; useful for lean-ingress spam/idempotency tests")
    ap.add_argument("--expect-status", choices=("verified", "received", "any"), default="verified", help="Expected receipt status. Use 'received' for lean ingress Phase 1.")
    ap.add_argument("--skip-weights", action="store_true", help="Skip V2 shadow weight check; required for lean ingress Phase 1")
    args = ap.parse_args()

    session = requests.Session()
    base = args.base.rstrip("/")
    challenge_base = (args.challenge_base or base).rstrip("/")
    submit_base = (args.submit_base or base).rstrip("/")
    kp, key_source = make_keypair(args)
    print(f"base={base}")
    print(f"challenge_base={challenge_base}")
    print(f"submit_base={submit_base}")
    print(f"hotkey={kp.ss58_address}")
    print(f"key_source={key_source}")

    r, body = get_json(session, challenge_base, "/health/live", timeout=20)
    print(f"challenge_health_live={r.status_code}")
    if r.status_code != 200:
        print(body)
        return 1
    if submit_base != challenge_base:
        r, body = get_json(session, submit_base, "/health/live", timeout=20)
        print(f"submit_health_live={r.status_code}")
        if r.status_code != 200:
            print(body)
            return 1

    r, payload = get_json(session, challenge_base, f"/v2/synthetic-boolean/per-miner/challenges?limit={int(args.limit)}", headers=read_headers(kp), timeout=30)
    print(f"challenges_status={r.status_code}")
    if r.status_code != 200 or not isinstance(payload, dict):
        print(json.dumps(payload, indent=2) if isinstance(payload, (dict, list)) else str(payload)[:1000])
        return 1
    print(f"submit_path={payload.get('submit_path')} count={payload.get('count')}")
    if payload.get("submit_path") != "/v2/agents/submit-bitset":
        print("E2E_FAILED submit_bitset_not_enabled")
        return 1
    items = payload.get("items") or []
    if not items:
        print("E2E_FAILED no_challenges")
        return 1
    item = items[0]
    challenge_id = str(item["challenge_id"])
    tier = int(item.get("tier") or 1)
    seq = int(item.get("seq") or 0)
    epoch = int(item.get("epoch") or payload.get("epoch") or 0)
    submit_token = str(item.get("submit_token") or "")
    print(f"selected={challenge_id} tier={tier} seq={seq} epoch={epoch} token={'yes' if submit_token else 'no'}")
    if not submit_token:
        print("E2E_FAILED missing_submit_token")
        return 1

    query = urlencode({"challenge_id": challenge_id, "tier": tier, "seq": seq})
    r = session.get(challenge_base + "/v2/synthetic-boolean/per-miner/cnf?" + query, headers=read_headers(kp), timeout=30)
    print(f"cnf_status={r.status_code} bytes={len(r.content)}")
    if r.status_code != 200:
        print(r.text[:1000])
        return 1
    cnf_text = r.text
    print(f"cnf_sha={hashlib.sha256(cnf_text.encode('utf-8')).hexdigest()[:12]}...")

    t0 = time.time()
    assignment, solver_used = solve_assignment(cnf_text, args.solver)
    solve_secs = time.time() - t0
    bitset = encode_bitset_assignment(assignment)
    assignment_b64 = base64.b64encode(bitset).decode("ascii")
    print(f"solved solver={solver_used} elapsed={solve_secs:.3f}s vars={len(assignment)} bitset_bytes={len(bitset)}")
    if args.dry_run:
        print("dry_run=1")
        return 0

    body = {
        "schema": BITSET_SCHEMA,
        "card_id": CARD_ID,
        "challenge_id": challenge_id,
        "submit_token": submit_token,
        "assignment_encoding": "bitset/v1",
        "assignment_b64": assignment_b64,
    }
    submit_results: list[dict[str, Any]] = []
    receipt_id = ""
    admit_ms_values: list[float] = []
    for i in range(max(1, int(args.repeat_submit))):
        t0 = time.time()
        r = session.post(submit_base + "/v2/agents/submit-bitset", json=body, headers=bitset_headers(kp, body), timeout=30)
        admit_ms = (time.time() - t0) * 1000.0
        admit_ms_values.append(admit_ms)
        print(f"submit_bitset_status[{i}]={r.status_code} admit_ms={admit_ms:.1f}")
        if r.status_code not in (200, 202):
            print(r.text[:1000])
            return 1
        receipt = r.json()
        submit_results.append(receipt)
        receipt_id = receipt_id or receipt["receipt_id"]
        print(f"receipt_id[{i}]={receipt.get('receipt_id')} status={receipt.get('status')} score={receipt.get('weighted_score')} replay={receipt.get('idempotent_replay')}")
    receipt = submit_results[-1]

    deadline = time.time() + max(1.0, float(args.poll_secs))
    final: dict[str, Any] | None = None
    if args.expect_status == "received":
        r, final_body = get_json(session, submit_base, f"/v2/agents/submit-bitset/receipts/{receipt_id}", timeout=20)
        if r.status_code == 200 and isinstance(final_body, dict):
            final = final_body
            print(f"receipt_state={final.get('status')} terminal={final.get('terminal')}")
        else:
            print(f"receipt_status={r.status_code} body={str(final_body)[:300]}")
            return 1
    else:
        while time.time() < deadline:
            r, final_body = get_json(session, submit_base, f"/v2/agents/submit-bitset/receipts/{receipt_id}", timeout=20)
            if r.status_code == 200 and isinstance(final_body, dict):
                final = final_body
                print(f"receipt_state={final.get('status')} terminal={final.get('terminal')}")
                if final.get("terminal") or args.expect_status == "any":
                    break
            else:
                print(f"receipt_status={r.status_code} body={str(final_body)[:300]}")
            time.sleep(1)
    if not final or (args.expect_status != "any" and final.get("status") != args.expect_status):
        print(f"E2E_FAILED receipt_not_{args.expect_status}")
        print(json.dumps(final or {}, indent=2, sort_keys=True))
        return 1

    if args.skip_weights:
        print("weights_check=skipped")
        print("E2E_OK " + json.dumps({
            "receipt_id": receipt_id,
            "status": final.get("status"),
            "hotkey": kp.ss58_address,
            "admit_ms_min": round(min(admit_ms_values), 1),
            "admit_ms_max": round(max(admit_ms_values), 1),
            "repeat_submit": max(1, int(args.repeat_submit)),
        }, sort_keys=True))
        return 0

    r, weights = get_json(session, challenge_base, "/v2/validator/weights/next", timeout=30)
    print(f"weights_status={r.status_code}")
    if r.status_code != 200 or not isinstance(weights, dict):
        print(weights)
        return 1
    row = next((w for w in weights.get("weights") or [] if w.get("miner_hotkey") == kp.ss58_address), None)
    if not row:
        print("E2E_FAILED hotkey_missing_from_shadow_weights")
        return 1
    print(f"shadow_weight={row.get('weight')} raw_score={row.get('raw_score')}")

    print("E2E_OK " + json.dumps({
        "receipt_id": receipt_id,
        "status": final.get("status"),
        "hotkey": kp.ss58_address,
        "weight": row.get("weight"),
        "raw_score": row.get("raw_score"),
        "admit_ms": round(admit_ms_values[-1], 1),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
