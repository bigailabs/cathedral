#!/usr/bin/env python3
"""Read-only Cathedral V2 relaunch preflight.

This script proves the system is ready to open, but it never opens the gate.
The actual open still requires the separate, explicit wrangler deploy with
V2_GATE_MODE=open-v2 after Fred says go.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SCRIPT = Path(__file__).resolve()
ROUTER_DIR = SCRIPT.parent
REPO = ROUTER_DIR.parent.parent
DEFAULT_BASE = "https://v2-beta.cathedral.computer"
DEFAULT_WEIGHTS_URL = "https://api.cathedral.computer/v1/validator/weights/next"
NON_CANARY_HOTKEY = "5NotACanaryHotkeyAtAll1111111111111111111111111"


@dataclass
class Result:
    status: str
    name: str
    detail: str = ""


class Preflight:
    def __init__(self) -> None:
        self.results: list[Result] = []

    def pass_(self, name: str, detail: str = "") -> None:
        self.results.append(Result("PASS", name, detail))
        print(f"PASS {name}" + (f" - {detail}" if detail else ""))

    def warn(self, name: str, detail: str = "") -> None:
        self.results.append(Result("WARN", name, detail))
        print(f"WARN {name}" + (f" - {detail}" if detail else ""))

    def fail(self, name: str, detail: str = "") -> None:
        self.results.append(Result("FAIL", name, detail))
        print(f"FAIL {name}" + (f" - {detail}" if detail else ""))

    def exit_code(self, warnings_as_errors: bool) -> int:
        if any(r.status == "FAIL" for r in self.results):
            return 1
        if warnings_as_errors and any(r.status == "WARN" for r in self.results):
            return 1
        return 0


def run(
    pf: Preflight,
    name: str,
    cmd: list[str],
    *,
    cwd: Path = REPO,
    timeout: int = 60,
    check_contains: str | None = None,
    check_not_contains: str | None = None,
) -> str | None:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except FileNotFoundError:
        pf.fail(name, f"command not found: {cmd[0]}")
        return None
    except subprocess.TimeoutExpired:
        pf.fail(name, f"timed out after {timeout}s")
        return None
    out = proc.stdout or ""
    if proc.returncode != 0:
        pf.fail(name, f"exit={proc.returncode}; last output: {out[-600:].strip()}")
        return out
    if check_contains and check_contains not in out:
        pf.fail(name, f"missing expected output {check_contains!r}")
        return out
    if check_not_contains and check_not_contains in out:
        pf.fail(name, f"unexpected output {check_not_contains!r}")
        return out
    pf.pass_(name)
    return out


def http_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> tuple[int, dict[str, str], Any, str]:
    request_headers = {
        "User-Agent": "cathedral-relaunch-preflight/1.0",
        "Accept": "application/json",
    }
    request_headers.update(headers or {})
    req = Request(url, headers=request_headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = int(resp.status)
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = int(exc.code)
        resp_headers = {k.lower(): v for k, v in exc.headers.items()}
    except URLError as exc:
        raise RuntimeError(str(exc)) from exc
    try:
        body: Any = json.loads(raw)
    except Exception:
        body = raw
    return status, resp_headers, body, raw


def iso_age_secs(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, datetime.now(timezone.utc).timestamp() - parsed.timestamp())


def select_python() -> str:
    explicit = os.environ.get("CATHEDRAL_PREFLIGHT_PYTHON", "").strip()
    if explicit:
        return explicit
    for candidate in (
        REPO / ".venv/bin/python",
        Path("/Users/dreamboat/Documents/PROJECTS/cathedralsubnet/.venv/bin/python"),
    ):
        if candidate.exists():
            return str(candidate)
    return shutil.which("python3") or "python3"


def check_git(pf: Preflight) -> None:
    out = run(pf, "git branch status", ["git", "status", "--short", "--branch"], timeout=20)
    if out is None:
        return
    lines = out.splitlines()
    branch = lines[0] if lines else ""
    tracked_dirty = [line for line in lines[1:] if not line.startswith("?? ")]
    untracked = [line for line in lines[1:] if line.startswith("?? ")]
    if tracked_dirty:
        pf.fail("tracked worktree clean", "; ".join(tracked_dirty[:6]))
    else:
        pf.pass_("tracked worktree clean", branch)
    if untracked:
        pf.warn("untracked files present", f"{len(untracked)} untracked; ignored by preflight")


def check_local(pf: Preflight, args: argparse.Namespace) -> None:
    check_git(pf)
    run(pf, "worker syntax", ["node", "--check", "deploy/v2-beta-router/worker.mjs"])
    run(pf, "worker staged/open tests", ["node", "deploy/v2-beta-router/worker.test.mjs"])
    run(
        pf,
        "env checker syntax",
        [select_python(), "-m", "py_compile", "deploy/check_env_surface.py"],
    )
    run(
        pf,
        "miner e2e script syntax",
        [select_python(), "-m", "py_compile", "scripts/v2_bitset_miner_e2e.py"],
    )
    if not args.skip_python_tests:
        run(
            pf,
            "focused launch pytest",
            [
                select_python(),
                "-m",
                "pytest",
                "scaffold/publisher/tests/test_real_instance_bitset_e2e.py",
                "scaffold/publisher/tests/test_solution_manifest_v2.py",
                "scaffold/publisher/tests/test_v2_pm_payout_bridge.py",
                "scaffold/publisher/tests/test_statement_timeout_guard.py",
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            timeout=args.pytest_timeout_secs,
        )
    if args.env_file:
        run(
            pf,
            "operator env audit",
            [select_python(), "deploy/check_env_surface.py", "--env-file", args.env_file],
        )


def check_wrangler(pf: Preflight, args: argparse.Namespace) -> None:
    if args.skip_wrangler:
        pf.warn("wrangler dry-run", "skipped by flag")
        return
    staged = run(
        pf,
        "wrangler dry-run staged",
        ["npx", "wrangler", "deploy", "--dry-run"],
        cwd=ROUTER_DIR,
        timeout=90,
        check_contains="env.V2_GATE_MODE",
        check_not_contains="env.routes",
    )
    if staged and 'env.V2_GATE_MODE ("staged")' not in staged:
        pf.warn("wrangler staged binding value", "expected staged binding was not visible")
    opened = run(
        pf,
        "wrangler dry-run open-v2",
        ["npx", "wrangler", "deploy", "--dry-run", "--var", "V2_GATE_MODE:open-v2"],
        cwd=ROUTER_DIR,
        timeout=90,
        check_contains="env.V2_GATE_MODE",
        check_not_contains="env.routes",
    )
    if opened:
        pf.pass_("open command remains explicit", "dry-run only; no deploy performed")


def check_live_gate(pf: Preflight, args: argparse.Namespace) -> None:
    if args.skip_live:
        pf.warn("live edge checks", "skipped by flag")
        return
    base = args.base.rstrip("/")
    miner_headers = {
        "x-cathedral-hotkey": NON_CANARY_HOTKEY,
        "x-cathedral-signature": "preflight-signature",
        "x-cathedral-submitted-at": "2026-07-08T00:00:00.000Z",
    }
    try:
        status, headers, body, _raw = http_json(
            f"{base}/v2/synthetic-boolean/per-miner/challenges?limit=1",
            headers=miner_headers,
        )
        reason = headers.get("x-cathedral-rejection-reason") or (
            body.get("reason") if isinstance(body, dict) else None)
        if status == 429 and reason == "v2_beta_staged_reopen":
            pf.pass_("non-canary V2 gate closed", "429 v2_beta_staged_reopen")
        else:
            pf.fail("non-canary V2 gate closed", f"status={status} reason={reason!r}")

        status, headers, body, _raw = http_json(
            f"{base}/v1/synthetic-boolean/per-miner/challenges?limit=1",
            headers=miner_headers,
        )
        reason = headers.get("x-cathedral-rejection-reason") or (
            body.get("reason") if isinstance(body, dict) else None)
        if status == 410 and reason == "v1_miner_path_retired":
            pf.pass_("V1 per-miner path retired", "410 v1_miner_path_retired")
        else:
            pf.fail("V1 per-miner path retired", f"status={status} reason={reason!r}")

        status, _headers, body, _raw = http_json(f"{base}/health/ready")
        if status == 200 and isinstance(body, dict) and body.get("status") == "ok":
            pf.pass_("public readiness", f"service_role={body.get('service_role')}")
        else:
            pf.fail("public readiness", f"status={status} body={str(body)[:300]}")

        status, _headers, body, _raw = http_json(f"{base}/v2/verify/metrics")
        if status == 200 and isinstance(body, dict) and body.get("enabled") is False:
            pf.pass_("public verifier disabled", f"pending={body.get('pending_count')}")
        else:
            enabled = body.get("enabled") if isinstance(body, dict) else None
            pf.fail("public verifier disabled", f"status={status} enabled={enabled!r}")
    except RuntimeError as exc:
        pf.fail("live edge checks", str(exc))


def check_weights(pf: Preflight, args: argparse.Namespace) -> None:
    if args.skip_live:
        return
    try:
        status, headers, body, _raw = http_json(args.weights_url)
    except RuntimeError as exc:
        pf.fail("validator weights fresh", str(exc))
        return
    if status != 200 or not isinstance(body, dict):
        pf.fail("validator weights fresh", f"status={status} body={str(body)[:300]}")
        return
    weights = body.get("weights") or []
    generated_at = str(body.get("generated_at") or "")
    if not generated_at or not isinstance(weights, list) or not weights:
        pf.fail("validator weights fresh", "missing generated_at or weights")
        return
    if len(weights) <= args.min_weight_count:
        pf.fail(
            "validator weights fresh",
            f"weights={len(weights)} <= min {args.min_weight_count}",
        )
        return
    try:
        age = iso_age_secs(generated_at)
    except Exception as exc:
        pf.fail("validator weights fresh", f"bad generated_at={generated_at!r}: {exc}")
        return
    source = headers.get("x-cathedral-vector-source", "unknown")
    if age <= args.max_weight_age_secs:
        pf.pass_("validator weights fresh", f"age={age:.0f}s weights={len(weights)} source={source}")
    else:
        pf.fail("validator weights fresh", f"age={age:.0f}s exceeds {args.max_weight_age_secs}s")


def check_ssh(pf: Preflight, args: argparse.Namespace) -> None:
    target = args.ssh_target or os.environ.get("CATHEDRAL_PREFLIGHT_SSH", "").strip()
    if not target:
        pf.warn("private verifier topology", "skipped; set CATHEDRAL_PREFLIGHT_SSH to check")
        return
    ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    key = args.ssh_key or os.environ.get("CATHEDRAL_PREFLIGHT_SSH_KEY", "").strip()
    if key:
        ssh_cmd += ["-i", str(Path(key).expanduser())]
    try:
        proc = subprocess.run(
            ssh_cmd + [target, "curl -fsS http://127.0.0.1:8000/v2/verify/metrics"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
    except Exception as exc:
        pf.fail("private verifier topology", str(exc))
        return
    if proc.returncode != 0:
        pf.fail("private verifier topology", proc.stdout[-500:].strip())
        return
    try:
        metrics = json.loads(proc.stdout)
    except Exception as exc:
        pf.fail("private verifier topology", f"bad metrics JSON: {exc}")
        return
    if metrics.get("enabled") is not True:
        pf.fail("private verifier topology", f"enabled={metrics.get('enabled')!r}")
        return
    if metrics.get("last_worker_error"):
        pf.fail("private verifier topology", f"last_worker_error={metrics.get('last_worker_error')!r}")
        return
    pending = int(metrics.get("pending_count") or 0)
    if pending > args.max_pending:
        pf.fail("private verifier topology", f"pending={pending} > {args.max_pending}")
        return
    pf.pass_("private verifier topology", f"enabled=true pending={pending} last_batch={metrics.get('last_batch_count')}")

    services = (
        "systemctl --user is-active cathedral-v2-beta-origin.service && "
        "systemctl is-active cathedral-publisher.service"
    )
    proc = subprocess.run(
        ssh_cmd + [target, services],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    if proc.returncode == 0 and proc.stdout.splitlines().count("active") >= 2:
        pf.pass_("sandbox services active")
    else:
        pf.fail("sandbox services active", proc.stdout[-500:].strip())

    proc = subprocess.run(
        ssh_cmd + [
            target,
            "curl -fsS http://127.0.0.1:8000/v1/validator/weights/next",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    if proc.returncode != 0:
        pf.fail("private weight vector cardinality", proc.stdout[-500:].strip())
    else:
        try:
            payload = json.loads(proc.stdout)
            count = len(payload.get("weights") or [])
            meta = payload.get("policy_metadata") or {}
            external = meta.get("external_scores") or {}
        except Exception as exc:
            pf.fail("private weight vector cardinality", f"bad JSON: {exc}")
        else:
            if count <= args.min_weight_count:
                pf.fail(
                    "private weight vector cardinality",
                    f"weights={count} <= min {args.min_weight_count}",
                )
            else:
                pf.pass_(
                    "private weight vector cardinality",
                    (
                        f"weights={count} "
                        f"mode={meta.get('effective_mode')} "
                        f"external_enabled={external.get('enabled')}"
                    ),
                )

    remote_dir = shlex.quote(args.ssh_cathedral_dir)
    remote_env = shlex.quote(args.ssh_env_file)
    remote_pm_coverage = (
        f"cd {remote_dir} && set -a && . {remote_env} && set +a && "
        f"CATHEDRAL_PREFLIGHT_PM_COVERAGE_HORIZON_HOURS={float(args.pm_coverage_horizon_hours)} "
        f"CATHEDRAL_PREFLIGHT_MIN_WEIGHT_COUNT={int(args.min_weight_count)} "
        f"CATHEDRAL_PREFLIGHT_MIN_LIVE_COVERAGE_RATIO={float(args.min_live_coverage_ratio)} "
        """.venv/bin/python - <<'PY'
import json
import os
from datetime import datetime, timedelta, timezone

from scaffold.publisher import weights as weights_mod
from scaffold.publisher.store import Store


def ms_iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def positive_scores(at: datetime) -> dict[str, float]:
    return {
        str(hotkey): float(value)
        for hotkey, value in weights_mod.compose_scores(store, now=at).items()
        if float(value) > 0.0
    }


def coverage_stats(at: datetime, scores: dict[str, float], live_hotkeys: set[str]) -> dict:
    since = ms_iso(at - timedelta(hours=window_hours))
    stats = {
        "since": since,
        "score_hotkeys": len(scores),
        "live_score_hotkeys": None,
    }
    if live_hotkeys:
        stats["live_score_hotkeys"] = sum(1 for hotkey in scores if hotkey in live_hotkeys)
    return stats


def chain_hotkeys() -> tuple[set[str], str | None]:
    try:
        import bittensor as bt
        network = os.environ.get(weights_mod.NETWORK_ENV, "finney")
        netuid = int(os.environ.get(weights_mod.NETUID_ENV, "39") or "39")
        sub = bt.subtensor(network=network) if hasattr(bt, "subtensor") else bt.Subtensor(network=network)
        mg = sub.metagraph(netuid=netuid)
        return set(str(hk) for hk in (getattr(mg, "hotkeys", []) or [])), None
    except Exception as exc:
        return set(), f"{type(exc).__name__}: {str(exc)[:160]}"


store = Store(os.environ.get("CATHEDRAL_DB_PATH", "cathedral.db"))
now = datetime.now(timezone.utc)
window_hours = float(weights_mod.window_hours())
horizon_hours = float(os.environ.get("CATHEDRAL_PREFLIGHT_PM_COVERAGE_HORIZON_HOURS", "18") or "18")
min_weight_count = int(os.environ.get("CATHEDRAL_PREFLIGHT_MIN_WEIGHT_COUNT", "300") or "300")
min_live_coverage_ratio = float(os.environ.get("CATHEDRAL_PREFLIGHT_MIN_LIVE_COVERAGE_RATIO", "0.85") or "0.85")
horizon_at = now + timedelta(hours=horizon_hours)
current_scores = positive_scores(now)
horizon_scores = positive_scores(horizon_at)
live_hotkeys, live_error = chain_hotkeys()
current = coverage_stats(now, current_scores, live_hotkeys)
horizon = coverage_stats(horizon_at, horizon_scores, live_hotkeys)
live_count = len(live_hotkeys)
current_live_ratio = (
    float(current["live_score_hotkeys"]) / live_count
    if live_count and current["live_score_hotkeys"] is not None else None
)
horizon_live_ratio = (
    float(horizon["live_score_hotkeys"]) / live_count
    if live_count and horizon["live_score_hotkeys"] is not None else None
)
scoring_mode = weights_mod.perminer_scoring_mode()
if live_count:
    coverage_ok = (
        current_live_ratio is not None
        and horizon_live_ratio is not None
        and current_live_ratio >= min_live_coverage_ratio
        and horizon_live_ratio >= min_live_coverage_ratio
    )
else:
    coverage_ok = (
        current["score_hotkeys"] > min_weight_count
        and horizon["score_hotkeys"] > min_weight_count
    )
payload = {
    "ok": (
        scoring_mode == "pm_primary"
        and coverage_ok
    ),
    "chain_hotkeys": live_count,
    "chain_error": live_error,
    "current_live_ratio": current_live_ratio,
    "horizon_live_ratio": horizon_live_ratio,
    "scoring_mode": scoring_mode,
    "window_hours": window_hours,
    "horizon_hours": horizon_hours,
    "min_weight_count": min_weight_count,
    "min_live_coverage_ratio": min_live_coverage_ratio,
    "now": current,
    "horizon": horizon,
}
print(json.dumps(payload, sort_keys=True))
raise SystemExit(0 if payload["ok"] else 1)
PY"""
    )
    proc = subprocess.run(
        ssh_cmd + [target, remote_pm_coverage],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=90,
    )
    try:
        payload = json.loads(proc.stdout.splitlines()[-1])
    except Exception as exc:
        pf.fail(
            "remote PM coverage horizon",
            f"bad JSON: {exc}; output={proc.stdout[-500:].strip()}",
        )
    else:
        now_payload = payload.get("now") or {}
        horizon_payload = payload.get("horizon") or {}
        chain_hotkeys = int(payload.get("chain_hotkeys") or 0)
        if chain_hotkeys:
            coverage_detail = (
                f"now_live={now_payload.get('live_score_hotkeys')}/{chain_hotkeys} "
                f"horizon_live={horizon_payload.get('live_score_hotkeys')}/{chain_hotkeys} "
                f"min_live_ratio={payload.get('min_live_coverage_ratio')}"
            )
        else:
            coverage_detail = (
                f"chain_unavailable={payload.get('chain_error')} "
                f"min_scores={payload.get('min_weight_count')}"
            )
        detail = (
            f"mode={payload.get('scoring_mode')} "
            f"window={payload.get('window_hours')}h "
            f"horizon={payload.get('horizon_hours')}h "
            f"now_scores={now_payload.get('score_hotkeys')} "
            f"horizon_scores={horizon_payload.get('score_hotkeys')} "
            f"{coverage_detail} "
            f"horizon_since={horizon_payload.get('since')}"
        )
        if proc.returncode == 0 and payload.get("ok") is True:
            pf.pass_("remote PM coverage horizon", detail)
        else:
            pf.fail(
                "remote PM coverage horizon",
                f"{detail}; widen CATHEDRAL_WEIGHTS_WINDOW_HOURS or refill fair V2 solves",
            )

    remote_audit = (
        f"cd {remote_dir} && "
        f".venv/bin/python deploy/check_env_surface.py --env-file {remote_env}"
    )
    proc = subprocess.run(
        ssh_cmd + [target, remote_audit],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    if proc.returncode != 0:
        pf.fail("remote env audit", proc.stdout[-800:].strip())
    else:
        warning_count = sum(
            1 for line in proc.stdout.splitlines() if line.startswith("  warn    ")
        )
        if warning_count:
            pf.warn(
                "remote env audit",
                f"no fatal errors; {warning_count} cleanup warnings",
            )
        else:
            pf.pass_("remote env audit", "no fatal errors")

    remote_pin = (
        f"cd {remote_dir} && set -a && . {remote_env} && set +a && "
        """.venv/bin/python - <<'PY'
import json
from scaffold.publisher import v2_pipeline

pin_ok = v2_pipeline.pin_v2_pm_env()
payload = {
    "pin_ok": pin_ok,
    "pinned": v2_pipeline._PM_ENV_PINNED,
    "v2_perminer_enabled": v2_pipeline.v2_perminer_enabled(),
}
print(json.dumps(payload, sort_keys=True))
raise SystemExit(0 if all(payload.values()) else 1)
PY"""
    )
    proc = subprocess.run(
        ssh_cmd + [target, remote_pin],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    if proc.returncode != 0:
        pf.fail("remote V2 env pin", proc.stdout[-800:].strip())
    else:
        try:
            payload = json.loads(proc.stdout.splitlines()[-1])
        except Exception as exc:
            pf.fail(
                "remote V2 env pin",
                f"bad JSON: {exc}; output={proc.stdout[-500:].strip()}",
            )
        else:
            pf.pass_(
                "remote V2 env pin",
                (
                    f"pin_ok={payload.get('pin_ok')} "
                    f"pinned={payload.get('pinned')} "
                    f"v2_perminer_enabled={payload.get('v2_perminer_enabled')}"
                ),
            )

    remote_bake_coverage = (
        f"cd {remote_dir} && set -a && . {remote_env} && set +a && "
        f"CATHEDRAL_PREFLIGHT_PREBAKE_DEPTH={int(args.min_prebake_depth)} "
        """.venv/bin/python - <<'PY'
import json
import os

from scaffold.publisher import per_miner as pm
from scaffold.publisher import v2_pipeline
from scaffold.publisher import weights as weights_mod
from scaffold.publisher.store import Store

depth = max(1, int(os.environ.get("CATHEDRAL_PREFLIGHT_PREBAKE_DEPTH", "10") or "10"))
v2_database_path = (
    os.environ.get("CATHEDRAL_V2_DATABASE_URL", "").strip()
    or os.environ.get("CATHEDRAL_V2_DB_PATH", "").strip()
)
if v2_database_path and v2_pipeline.pm_payout_bridge_enabled():
    payload = {
        "ok": False,
        "reason": "split_v2_store_with_pm_payout_bridge",
        "store_source": "v2",
    }
    print(json.dumps(payload, sort_keys=True))
    raise SystemExit(1)
if v2_database_path:
    store = Store(v2_database_path, prefer_env_database_url=False)
    store_source = "v2"
else:
    store = Store(os.environ.get("CATHEDRAL_DB_PATH", "cathedral.db"))
    store_source = "main"

rows = store.query("SELECT DISTINCT hotkey FROM metagraph_hotkeys")
hotkeys = sorted({str(r["hotkey"]) for r in rows})
identities = sorted({
    weights_mod.scoring_identity_for_hotkey(store, hk, require_mapped=False) or hk
    for hk in hotkeys
})
epoch = pm.current_epoch()
expected_ids = []
for identity in identities:
    for tier in pm.TIERS:
        for seq in range(min(depth, pm.allotment_for(tier))):
            expected_ids.append(pm.instance_id(identity, epoch, tier, seq))

present = set()
for idx in range(0, len(expected_ids), 500):
    chunk = expected_ids[idx:idx + 500]
    if not chunk:
        continue
    placeholders = ",".join("?" for _ in chunk)
    q = f"SELECT challenge_id FROM v2_cnf_store WHERE challenge_id IN ({placeholders})"
    for row in store.query(q, tuple(chunk)):
        present.add(str(row["challenge_id"]))

missing = [cid for cid in expected_ids if cid not in present]
payload = {
    "ok": bool(expected_ids) and not missing,
    "store_backend": store.backend,
    "store_source": store_source,
    "epoch": epoch,
    "depth": depth,
    "hotkeys": len(hotkeys),
    "identities": len(identities),
    "expected": len(expected_ids),
    "present": len(present),
    "missing": len(missing),
    "missing_samples": missing[:5],
}
print(json.dumps(payload, sort_keys=True))
raise SystemExit(0 if payload["ok"] else 1)
PY"""
    )
    proc = subprocess.run(
        ssh_cmd + [target, remote_bake_coverage],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
    )
    try:
        payload = json.loads(proc.stdout.splitlines()[-1])
    except Exception as exc:
        pf.fail(
            "remote CNF prebake coverage",
            f"bad JSON: {exc}; output={proc.stdout[-500:].strip()}",
        )
    else:
        detail = (
            f"epoch={payload.get('epoch')} depth={payload.get('depth')} "
            f"present={payload.get('present')}/{payload.get('expected')} "
            f"store={payload.get('store_source')}/{payload.get('store_backend')}"
        )
        if proc.returncode == 0 and payload.get("ok") is True:
            pf.pass_("remote CNF prebake coverage", detail)
        else:
            missing_samples = payload.get("missing_samples") or []
            reason = payload.get("reason") or f"missing={payload.get('missing')}"
            pf.fail(
                "remote CNF prebake coverage",
                f"{detail} {reason} samples={missing_samples}",
            )


def check_e2e(pf: Preflight, args: argparse.Namespace) -> None:
    run_e2e = args.run_e2e or os.environ.get("CATHEDRAL_PREFLIGHT_RUN_E2E", "").strip().lower() in {"1", "true", "yes", "on"}
    if not run_e2e:
        pf.warn("canary E2E", "skipped by default; set --run-e2e to submit/replay")
        return
    run(
        pf,
        "canary V2 bitset E2E",
        [
            select_python(),
            "scripts/v2_bitset_miner_e2e.py",
            "--base",
            args.base.rstrip("/"),
            "--uri",
            args.e2e_uri,
            "--limit",
            "1",
        ],
        timeout=args.e2e_timeout_secs,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=os.environ.get("CATHEDRAL_PREFLIGHT_BASE", DEFAULT_BASE))
    ap.add_argument("--weights-url", default=os.environ.get("CATHEDRAL_PREFLIGHT_WEIGHTS_URL", DEFAULT_WEIGHTS_URL))
    ap.add_argument("--env-file", default=os.environ.get("CATHEDRAL_PREFLIGHT_ENV_FILE", ""))
    ap.add_argument("--ssh-target", default="")
    ap.add_argument("--ssh-key", default="")
    ap.add_argument(
        "--ssh-cathedral-dir",
        default=os.environ.get(
            "CATHEDRAL_PREFLIGHT_SSH_CATHEDRAL_DIR",
            "/home/polaris/cathedral",
        ),
    )
    ap.add_argument(
        "--ssh-env-file",
        default=os.environ.get("CATHEDRAL_PREFLIGHT_SSH_ENV_FILE", ".env.sh"),
    )
    ap.add_argument("--max-pending", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_MAX_PENDING", "0") or "0"))
    ap.add_argument("--max-weight-age-secs", type=float, default=float(os.environ.get("CATHEDRAL_PREFLIGHT_MAX_WEIGHT_AGE_SECS", "900") or "900"))
    ap.add_argument("--min-weight-count", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_MIN_WEIGHT_COUNT", "300") or "300"))
    ap.add_argument("--min-prebake-depth", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_MIN_PREBAKE_DEPTH", "10") or "10"))
    ap.add_argument("--pm-coverage-horizon-hours", type=float, default=float(os.environ.get("CATHEDRAL_PREFLIGHT_PM_COVERAGE_HORIZON_HOURS", "18") or "18"))
    ap.add_argument("--min-live-coverage-ratio", type=float, default=float(os.environ.get("CATHEDRAL_PREFLIGHT_MIN_LIVE_COVERAGE_RATIO", "0.85") or "0.85"))
    ap.add_argument("--pytest-timeout-secs", type=int, default=120)
    ap.add_argument("--e2e-timeout-secs", type=int, default=120)
    ap.add_argument("--e2e-uri", default=os.environ.get("CATHEDRAL_PREFLIGHT_E2E_URI", "//Alice"))
    ap.add_argument("--skip-python-tests", action="store_true")
    ap.add_argument("--skip-wrangler", action="store_true")
    ap.add_argument("--skip-live", action="store_true")
    ap.add_argument("--run-e2e", action="store_true")
    ap.add_argument("--warnings-as-errors", action="store_true")
    return ap.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    pf = Preflight()
    print("Cathedral V2 relaunch preflight")
    print(f"repo={REPO}")
    print(f"base={args.base.rstrip('/')}")
    print("mode=read-only; this script does not deploy or open the gate")
    print()

    start = time.time()
    check_local(pf, args)
    check_wrangler(pf, args)
    check_live_gate(pf, args)
    check_weights(pf, args)
    check_ssh(pf, args)
    check_e2e(pf, args)

    elapsed = time.time() - start
    counts = {status: sum(1 for r in pf.results if r.status == status) for status in ("PASS", "WARN", "FAIL")}
    print()
    print(f"summary pass={counts['PASS']} warn={counts['WARN']} fail={counts['FAIL']} elapsed={elapsed:.1f}s")
    if counts["FAIL"]:
        print("PRECHECK_FAILED")
    else:
        print("PRECHECK_OK")
    return pf.exit_code(args.warnings_as_errors)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
