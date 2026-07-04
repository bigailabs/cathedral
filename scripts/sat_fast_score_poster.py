#!/usr/bin/env python3
"""SAT fast-path score poster — the Release-1 "10% reward wire".

Cathedral's own verified fast-path SAT scoreboard
(``GET /v2/validator/weights/next`` on v2-beta) is not yet part of the real
weight vector. Rather than building a second blend, this script treats that
scoreboard as just another external mechanism: it reads the scoreboard and
POSTs it to the publisher's already-hardened external-scores intake
(``POST /v1/external-scores/violet``, ``source=cathedral_sat_fast``), which
``scaffold/publisher/weights.py::_apply_external_scores`` already blends into
the real signed vector — registration-gated, fraction-capped, fail-closed.
This script never touches chain, never sets weights, and never talks to the
blend directly; it only posts a report the same way Violet does.

Flow:
    v2 scoreboard (GET) -> normalize to an external-scores report
                          -> POST /v1/external-scores/violet (bearer + optional HMAC)

Usage:
    python scripts/sat_fast_score_poster.py --dry-run
    python scripts/sat_fast_score_poster.py --once
    python scripts/sat_fast_score_poster.py --loop --interval 300

Env:
    CATHEDRAL_V2_SCOREBOARD_URL   overrides --challenge-base
    CATHEDRAL_PUBLISHER_URL       overrides --submit-base
    CATHEDRAL_EXTERNAL_SCORES_TOKEN         bearer token for the intake (required to POST for real)
    CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET   optional raw-body HMAC secret

Design note: all parsing/normalization logic lives in pure functions
(``normalize_scores``, ``build_report``) that take an already-fetched
scoreboard dict, so the report shape is unit-testable with no network.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

SOURCE = "cathedral_sat_fast"

DEFAULT_SCOREBOARD_URL = "https://v2-beta.cathedral.computer/v2/validator/weights/next"
SCOREBOARD_URL_ENV = "CATHEDRAL_V2_SCOREBOARD_URL"

DEFAULT_SUBMIT_BASE = "https://api.cathedral.computer"
PUBLISHER_URL_ENV = "CATHEDRAL_PUBLISHER_URL"
SUBMIT_PATH = "/v1/external-scores/violet"

TOKEN_ENV = "CATHEDRAL_EXTERNAL_SCORES_TOKEN"
HMAC_SECRET_ENV = "CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET"


# --------------------------------------------------------------------------
# Pure parsing / normalization logic (unit-tested with no network)
# --------------------------------------------------------------------------
def _ms_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def normalize_scores(entries: list[Any]) -> list[tuple[str, float]]:
    """Extract (miner_hotkey, positive raw value) pairs from the v2 `weights`
    list. Prefers `raw_score`; falls back to `weight`. Skips empty hotkeys and
    non-positive / non-finite values."""
    out: list[tuple[str, float]] = []
    if not isinstance(entries, list):
        return out
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        hotkey = str(entry.get("miner_hotkey") or "").strip()
        if not hotkey or hotkey in seen:
            continue
        value = _positive_float(entry.get("raw_score"))
        if value is None:
            value = _positive_float(entry.get("weight"))
        if value is None:
            continue
        seen.add(hotkey)
        out.append((hotkey, value))
    return out


def _positive_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out <= 0.0:
        return None
    return out


def build_report(
    scoreboard: dict[str, Any],
    *,
    now: datetime,
    source: str = SOURCE,
) -> dict[str, Any] | None:
    """Map a v2 scoreboard payload to an external-scores report.

    Returns None for an empty/degraded scoreboard (no weights, or nothing with
    a positive score) — callers should treat that as "post nothing", not an
    error.
    """
    if not isinstance(scoreboard, dict):
        return None
    candidates = normalize_scores(scoreboard.get("weights"))
    if not candidates:
        return None
    max_value = max(v for _, v in candidates)
    if max_value <= 0.0:
        return None

    scores: list[dict[str, Any]] = []
    for hotkey, value in candidates:
        norm = value / max_value
        norm = max(0.0, min(1.0, norm))
        if norm <= 0.0:
            continue
        scores.append({"miner_hotkey": hotkey, "score": round(norm, 9)})
    if not scores:
        return None

    try:
        epoch = int(scoreboard.get("epoch") or 0)
    except (TypeError, ValueError):
        epoch = 0

    return {
        "source": source,
        "mechanism": source,
        "epoch": epoch,
        "generated_at": _ms_iso(now),
        "scores": scores,
        "metadata": {
            "upstream_vector_id": scoreboard.get("vector_id"),
            "upstream_generated_at": scoreboard.get("generated_at"),
            "upstream_policy_version": scoreboard.get("policy_version"),
            "upstream_policy_reason": scoreboard.get("policy_reason"),
        },
    }


def compute_hmac_header(body: bytes, secret: str) -> str:
    """Same scheme external_scores.verify_hmac expects: sha256=<hex hmac>."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def canonical_body(report: dict[str, Any]) -> bytes:
    return json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")


# --------------------------------------------------------------------------
# I/O layer (network) — kept thin so the logic above stays testable
# --------------------------------------------------------------------------
def fetch_scoreboard(url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """GET the v2 scoreboard. Returns {status, body(dict|None), error}. No raise."""
    req = urllib.request.Request(
        url, method="GET", headers={"User-Agent": "cathedral-sat-fast-poster"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = None
            return {"status": int(resp.status), "body": body, "error": None}
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = None
        return {"status": int(e.code), "body": body, "error": None}
    except Exception as e:
        return {"status": None, "body": None, "error": f"{type(e).__name__}: {e}"}


def post_report(
    url: str,
    report: dict[str, Any],
    *,
    token: str | None,
    hmac_secret: str | None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """POST the report. Returns {status, body(dict|None), error}. No raise.

    Never logs `token` / `hmac_secret` values — only whether they were set.
    """
    body = canonical_body(report)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if hmac_secret:
        headers["X-Cathedral-External-Signature"] = compute_hmac_header(body, hmac_secret)
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except Exception:
                parsed = None
            return {"status": int(resp.status), "body": parsed, "error": None}
    except urllib.error.HTTPError as e:
        try:
            parsed = json.loads(e.read().decode("utf-8"))
        except Exception:
            parsed = None
        return {"status": int(e.code), "body": parsed, "error": None}
    except Exception as e:
        return {"status": None, "body": None, "error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
def run_once(args: argparse.Namespace) -> int:
    fetched = fetch_scoreboard(args.challenge_base, timeout=args.timeout)
    if fetched["error"] is not None or fetched["status"] is None:
        print(f"[sat_fast_poster] scoreboard fetch failed: {fetched['error']}")
        return 1
    if fetched["status"] >= 400:
        print(f"[sat_fast_poster] scoreboard fetch failed: status={fetched['status']}")
        return 1
    body = fetched["body"]
    if not isinstance(body, dict):
        print("[sat_fast_poster] scoreboard returned no JSON body")
        return 1

    report = build_report(body, now=datetime.now(timezone.utc), source=args.source)
    if report is None:
        print("[sat_fast_poster] scoreboard empty/degraded (no positive scores) "
              "-> posting nothing")
        return 0

    print(f"[sat_fast_poster] built report: source={report['source']} "
          f"epoch={report['epoch']} scores={len(report['scores'])}")

    if args.dry_run:
        print(json.dumps(report, indent=2, sort_keys=True))
        print("[sat_fast_poster] --dry-run: not posting")
        return 0

    token = args.token
    hmac_secret = args.hmac_secret
    print(f"[sat_fast_poster] auth: token={'set' if token else 'MISSING'} "
          f"hmac={'set' if hmac_secret else 'unset'}")

    submit_url = args.submit_base.rstrip("/") + SUBMIT_PATH
    result = post_report(submit_url, report, token=token, hmac_secret=hmac_secret,
                          timeout=args.timeout)
    if result["error"] is not None or result["status"] is None:
        print(f"[sat_fast_poster] POST failed: {result['error']}")
        return 1
    if result["status"] >= 300:
        print(f"[sat_fast_poster] POST rejected: status={result['status']} "
              f"body={result['body']}")
        return 1
    print(f"[sat_fast_poster] POST accepted: status={result['status']} "
          f"body={result['body']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Post Cathedral's verified fast-path SAT scoreboard into "
                     "the hardened external-scores blend (Release-1 10%% wire).")
    p.add_argument("--challenge-base", default=None,
                   help=f"v2 scoreboard URL to GET (default env {SCOREBOARD_URL_ENV} "
                        f"or {DEFAULT_SCOREBOARD_URL})")
    p.add_argument("--submit-base", default=None,
                   help=f"publisher base URL; {SUBMIT_PATH} is appended (default env "
                        f"{PUBLISHER_URL_ENV} or {DEFAULT_SUBMIT_BASE})")
    p.add_argument("--source", default=SOURCE,
                   help=f"external-scores source label (default {SOURCE})")
    p.add_argument("--dry-run", action="store_true",
                   help="print the built report and exit; never POSTs")
    p.add_argument("--loop", action="store_true",
                   help="run forever, sleeping --interval seconds between polls")
    p.add_argument("--interval", type=float, default=300.0,
                   help="seconds between polls when --loop (default 300)")
    p.add_argument("--timeout", type=float, default=10.0)
    args = p.parse_args(argv)

    args.challenge_base = (
        args.challenge_base
        or os.environ.get(SCOREBOARD_URL_ENV, "").strip()
        or DEFAULT_SCOREBOARD_URL
    )
    args.submit_base = (
        args.submit_base
        or os.environ.get(PUBLISHER_URL_ENV, "").strip()
        or DEFAULT_SUBMIT_BASE
    )
    args.token = (os.environ.get(TOKEN_ENV) or "").strip() or None
    args.hmac_secret = (os.environ.get(HMAC_SECRET_ENV) or "").strip() or None

    if not args.loop:
        return run_once(args)

    print(f"[sat_fast_poster] --loop: polling every {args.interval:.0f}s "
          f"(ctrl-C to stop)")
    exit_code = 0
    try:
        while True:
            try:
                exit_code = run_once(args)
            except Exception as exc:  # keep the loop alive across transient errors
                print(f"[sat_fast_poster] iteration failed: {exc!r}")
                exit_code = 1
            time.sleep(max(1.0, args.interval))
    except KeyboardInterrupt:
        print("[sat_fast_poster] stopped")
        return 0
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
