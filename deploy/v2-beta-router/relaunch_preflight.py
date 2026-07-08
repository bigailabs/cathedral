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
TRUTHY = {"1", "true", "yes", "on"}


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


def env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUTHY


def check_launch_intent(pf: Preflight, args: argparse.Namespace) -> None:
    intent = args.launch_intent
    if intent == "staged":
        pf.pass_("launch intent", "staged/closed-gate readiness profile")
        return

    missing: list[str] = []
    if args.skip_live:
        missing.append("live edge checks must not be skipped")
    if args.skip_wrangler:
        missing.append("wrangler dry-runs must not be skipped")
    if not (args.ssh_target or os.environ.get("CATHEDRAL_PREFLIGHT_SSH", "").strip()):
        missing.append("CATHEDRAL_PREFLIGHT_SSH or --ssh-target")
    if not (args.run_e2e or env_truthy("CATHEDRAL_PREFLIGHT_RUN_E2E")):
        missing.append("--run-e2e")
    if not (args.run_capacity_probe or env_truthy("CATHEDRAL_PREFLIGHT_RUN_CAPACITY_PROBE")):
        missing.append("--run-capacity-probe")
    if not (args.run_edge_soak or env_truthy("CATHEDRAL_PREFLIGHT_RUN_EDGE_SOAK")):
        missing.append("--run-edge-soak")
    if args.prebake_epoch_lookahead < 1:
        missing.append("--prebake-epoch-lookahead 1")
    if not str(args.expected_v2_real_fraction).strip():
        missing.append("--expected-v2-real-fraction {0 or 0.10}")

    if missing:
        pf.fail("all-miner launch intent prerequisites", "; ".join(missing))
    else:
        pf.pass_(
            "all-miner launch intent prerequisites",
            (
                "e2e+capacity+edge-soak required; "
                f"prebake_lookahead={args.prebake_epoch_lookahead}; "
                f"expected_v2_real_fraction={args.expected_v2_real_fraction}"
            ),
        )


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
    check_launch_intent(pf, args)
    run(pf, "worker syntax", ["node", "--check", "deploy/v2-beta-router/worker.mjs"])
    run(pf, "worker staged/open tests", ["node", "deploy/v2-beta-router/worker.test.mjs"])
    run(
        pf,
        "env checker syntax",
        [
            select_python(),
            "-m",
            "py_compile",
            "deploy/check_env_surface.py",
            "deploy/check_env_template.py",
        ],
    )
    run(
        pf,
        "miner e2e script syntax",
        [
            select_python(),
            "-m",
            "py_compile",
            "scripts/v2_bitset_miner_e2e.py",
            "scripts/v2_bitset_capacity_probe.py",
            "scripts/v2_edge_staged_soak.py",
        ],
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
        run(
            pf,
            "operator env template",
            [
                select_python(),
                "deploy/check_env_template.py",
                "--template",
                args.env_template,
                "--env-file",
                args.env_file,
            ],
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
    remote_dir = shlex.quote(args.ssh_cathedral_dir)
    remote_env = shlex.quote(args.ssh_env_file)
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
    oldest_pending_age = metrics.get("oldest_pending_age_secs")
    if oldest_pending_age is not None:
        try:
            oldest_pending_age_f = float(oldest_pending_age)
        except (TypeError, ValueError):
            pf.fail(
                "private verifier topology",
                f"bad oldest_pending_age_secs={oldest_pending_age!r}",
            )
            return
        if oldest_pending_age_f > args.max_oldest_pending_age_secs:
            pf.fail(
                "private verifier topology",
                f"oldest_pending_age={oldest_pending_age_f:.1f}s "
                f"> {args.max_oldest_pending_age_secs}s",
            )
            return
    age_detail = (
        "none" if oldest_pending_age is None else f"{float(oldest_pending_age):.1f}s"
    )
    pf.pass_(
        "private verifier topology",
        f"enabled=true pending={pending} oldest_pending_age={age_detail} "
        f"last_batch={metrics.get('last_batch_count')}",
    )

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

    remote_process_env = (
        f"cd {remote_dir} && set -a && . {remote_env} && set +a && "
        """.venv/bin/python - <<'PY'
import json
import os
from pathlib import Path

CORE_KEYS = (
    "CATHEDRAL_PM_READ_HARD_CAP",
    "CATHEDRAL_V2_READ_THREADS",
    "CATHEDRAL_V2_SUBMIT_BITSET_THREADS",
    "CATHEDRAL_V2_VERIFY_BATCH_SIZE",
    "CATHEDRAL_V2_BITSET_VERIFY_THREADS",
    "CATHEDRAL_SUBMIT_HARD_CAP",
    "CATHEDRAL_SUBMIT_MAX_CONCURRENCY",
    "CATHEDRAL_PG_STATEMENT_TIMEOUT_MS",
    "CATHEDRAL_V2_REAL_FRACTION",
    "CATHEDRAL_WEIGHTS_WINDOW_HOURS",
)


def read_proc_env(pid: str) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    data = {}
    for item in raw.split(b"\\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        data[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return data


def proc_for_port(port: str) -> tuple[str | None, list[str]]:
    matches = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            cmdline = (path / "cmdline").read_bytes().replace(b"\\0", b" ").decode()
        except Exception:
            continue
        if (
            "uvicorn" in cmdline
            and "scaffold.publisher.server:app" in cmdline
            and f"--port {port}" in cmdline
        ):
            matches.append(path.name)
    return (matches[0] if len(matches) == 1 else None), matches


private_pid, private_matches = proc_for_port("8000")
public_pid, public_matches = proc_for_port("8080")
errors = []
private_env = read_proc_env(private_pid) if private_pid else {}
public_env = read_proc_env(public_pid) if public_pid else {}
if private_pid is None:
    errors.append(f"private_pid_matches={private_matches}")
if public_pid is None:
    errors.append(f"public_pid_matches={public_matches}")

expected_private = {key: str(os.environ.get(key, "")) for key in CORE_KEYS}
expected_public = dict(expected_private)
expected_public["CATHEDRAL_PM_READ_HARD_CAP"] = str(
    os.environ.get("CATHEDRAL_V2_PUBLIC_READ_HARD_CAP", "8") or "8"
)
expected_public["CATHEDRAL_V2_READ_THREADS"] = str(
    os.environ.get("CATHEDRAL_V2_PUBLIC_READ_THREADS", "4") or "4"
)

private_actual = {key: private_env.get(key, "") for key in CORE_KEYS}
public_actual = {key: public_env.get(key, "") for key in CORE_KEYS}
for key, expected in expected_private.items():
    if private_actual.get(key) != expected:
        errors.append(f"private {key}={private_actual.get(key)!r} expected {expected!r}")
for key, expected in expected_public.items():
    if public_actual.get(key) != expected:
        errors.append(f"public {key}={public_actual.get(key)!r} expected {expected!r}")
if private_env.get("CATHEDRAL_V2_VERIFY_WORKER_ENABLED") != "1":
    errors.append("private verifier worker env is not 1")
if public_env.get("CATHEDRAL_V2_VERIFY_WORKER_ENABLED") != "0":
    errors.append("public verifier worker env is not 0")

payload = {
    "ok": not errors,
    "errors": errors,
    "private_pid": private_pid,
    "public_pid": public_pid,
    "private": private_actual,
    "public": public_actual,
    "private_worker": private_env.get("CATHEDRAL_V2_VERIFY_WORKER_ENABLED"),
    "public_worker": public_env.get("CATHEDRAL_V2_VERIFY_WORKER_ENABLED"),
}
print(json.dumps(payload, sort_keys=True))
raise SystemExit(0 if payload["ok"] else 1)
PY"""
    )
    proc = subprocess.run(
        ssh_cmd + [target, remote_process_env],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    try:
        payload = json.loads(proc.stdout.splitlines()[-1])
    except Exception as exc:
        pf.fail(
            "remote process env parity",
            f"bad JSON: {exc}; output={proc.stdout[-500:].strip()}",
        )
    else:
        if proc.returncode == 0 and payload.get("ok") is True:
            detail = (
                f"private_pid={payload.get('private_pid')} worker={payload.get('private_worker')} "
                f"public_pid={payload.get('public_pid')} worker={payload.get('public_worker')}"
            )
            pf.pass_("remote process env parity", detail)
        else:
            pf.fail("remote process env parity", f"errors={payload.get('errors')}")

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

    remote_vector_continuity = (
        f"cd {remote_dir} && set -a && . {remote_env} && set +a && "
        f"CATHEDRAL_PREFLIGHT_MIN_POLICY_VERSION={int(args.min_policy_version)} "
        """.venv/bin/python - <<'PY'
import json
import os
from datetime import datetime, timezone
from urllib.request import urlopen

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scaffold import wire_vector as wire
from scaffold.publisher import weights as weights_mod
from scaffold.publisher.store import Store


def ms_iso_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


store = Store(os.environ.get("CATHEDRAL_DB_PATH", "cathedral.db"))
state_rows = store.query(
    "SELECT last_policy_version FROM weight_policy_state WHERE id = 1"
)
vector_rows = store.query(
    "SELECT generated_at_iso, policy_version, vector_json, updated_at_iso "
    "FROM signed_weight_vectors WHERE id = ?",
    ("latest",),
)
with urlopen("http://127.0.0.1:8000/v1/validator/weights/next", timeout=20) as resp:
    endpoint_vector = json.loads(resp.read().decode("utf-8"))

signing_key_hex = (
    os.environ.get(weights_mod.SIGNING_KEY_ENV, "").strip()
    or os.environ.get("CATHEDRAL_EVAL_SIGNING_KEY", "").strip()
)
key_id = os.environ.get(weights_mod.KEY_ID_ENV, "cathedral-weight-policy")
network = os.environ.get(weights_mod.NETWORK_ENV, "finney")
netuid = int(os.environ.get(weights_mod.NETUID_ENV, "39") or "39")
min_policy_version = int(os.environ.get("CATHEDRAL_PREFLIGHT_MIN_POLICY_VERSION", "1700000000000") or "1700000000000")
checks = {
    "state_row_present": bool(state_rows),
    "latest_vector_present": bool(vector_rows),
    "signing_key_present": bool(signing_key_hex),
}
errors = []
public_key_hex = None
persisted_vector = None
state_version = None
persisted_version = None
endpoint_version = int(endpoint_vector.get("policy_version") or 0)

try:
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(signing_key_hex.strip()))
    public_key_hex = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
except Exception as exc:
    checks["signing_key_derives_public_key"] = False
    errors.append(f"signing_key_derives_public_key: {type(exc).__name__}: {exc}")
else:
    checks["signing_key_derives_public_key"] = True

if state_rows:
    state_version = int(state_rows[0]["last_policy_version"] or 0)
if vector_rows:
    persisted_version = int(vector_rows[0]["policy_version"] or 0)
    persisted_vector = json.loads(vector_rows[0]["vector_json"])

checks["state_latest_match"] = (
    bool(state_version)
    and bool(persisted_version)
    and state_version == persisted_version
    and persisted_vector is not None
    and int(persisted_vector.get("policy_version") or 0) == persisted_version
)
checks["policy_version_epoch_ms_floor"] = (
    bool(state_version)
    and bool(persisted_version)
    and bool(endpoint_version)
    and min(state_version, persisted_version, endpoint_version) >= min_policy_version
)
if public_key_hex:
    for label, vector in (("persisted", persisted_vector), ("endpoint", endpoint_vector)):
        try:
            if not isinstance(vector, dict):
                raise wire.VectorError("missing vector")
            wire.verify_signature(
                vector,
                public_key_hex=public_key_hex,
                expected_key_id=key_id,
            )
            wire.invariant_check(
                vector,
                network=network,
                netuid=netuid,
                now_iso=ms_iso_now(),
            )
        except Exception as exc:
            checks[f"{label}_signature_and_invariants"] = False
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
        else:
            checks[f"{label}_signature_and_invariants"] = True

payload = {
    "ok": all(checks.values()),
    "checks": checks,
    "errors": errors,
    "key_id": key_id,
    "network": network,
    "netuid": netuid,
    "state_version": state_version,
    "persisted_version": persisted_version,
    "endpoint_version": endpoint_version,
    "min_policy_version": min_policy_version,
}
print(json.dumps(payload, sort_keys=True))
raise SystemExit(0 if payload["ok"] else 1)
PY"""
    )
    proc = subprocess.run(
        ssh_cmd + [target, remote_vector_continuity],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    try:
        payload = json.loads(proc.stdout.splitlines()[-1])
    except Exception as exc:
        pf.fail(
            "remote vector continuity",
            f"bad JSON: {exc}; output={proc.stdout[-500:].strip()}",
        )
    else:
        detail = (
            f"state={payload.get('state_version')} "
            f"persisted={payload.get('persisted_version')} "
            f"endpoint={payload.get('endpoint_version')} "
            f"key_id={payload.get('key_id')}"
        )
        if proc.returncode == 0 and payload.get("ok") is True:
            pf.pass_("remote vector continuity", detail)
        else:
            failed = [
                name for name, ok in (payload.get("checks") or {}).items() if not ok
            ]
            pf.fail(
                "remote vector continuity",
                f"{detail}; failed={failed}; errors={payload.get('errors')}",
            )

    remote_guardrails = (
        f"cd {remote_dir} && set -a && . {remote_env} && set +a && "
        f"CATHEDRAL_PREFLIGHT_MAX_PM_READ_HARD_CAP={int(args.max_pm_read_hard_cap)} "
        f"CATHEDRAL_PREFLIGHT_MAX_V2_READ_THREADS={int(args.max_v2_read_threads)} "
        f"CATHEDRAL_PREFLIGHT_MAX_V2_SUBMIT_BITSET_THREADS={int(args.max_v2_submit_bitset_threads)} "
        f"CATHEDRAL_PREFLIGHT_MAX_V2_VERIFY_BATCH_SIZE={int(args.max_v2_verify_batch_size)} "
        f"CATHEDRAL_PREFLIGHT_MAX_V2_BITSET_VERIFY_THREADS={int(args.max_v2_bitset_verify_threads)} "
        f"CATHEDRAL_PREFLIGHT_MAX_SUBMIT_HARD_CAP={int(args.max_submit_hard_cap)} "
        f"CATHEDRAL_PREFLIGHT_MAX_SUBMIT_MAX_CONCURRENCY={int(args.max_submit_max_concurrency)} "
        f"CATHEDRAL_PREFLIGHT_MAX_PG_STATEMENT_TIMEOUT_MS={int(args.max_pg_statement_timeout_ms)} "
        f"CATHEDRAL_PREFLIGHT_MIN_WEIGHTS_WINDOW_HOURS={float(args.min_weights_window_hours)} "
        f"CATHEDRAL_PREFLIGHT_EXPECTED_V2_REAL_FRACTION={shlex.quote(str(args.expected_v2_real_fraction).strip())} "
        f"CATHEDRAL_PREFLIGHT_REQUIRE_V2_SUBMIT_BACKPRESSURE={1 if args.launch_intent == 'all-miner-open' else 0} "
        f"CATHEDRAL_PREFLIGHT_MAX_V2_SUBMIT_BACKPRESSURE_PENDING={int(args.max_v2_submit_backpressure_pending)} "
        f"CATHEDRAL_PREFLIGHT_MAX_V2_SUBMIT_BACKPRESSURE_OLDEST_AGE_SECS={float(args.max_v2_submit_backpressure_oldest_age_secs)} "
        """.venv/bin/python - <<'PY'
import json
import os

from scaffold.publisher import weights as weights_mod


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


pm_read_hard_cap = env_int("CATHEDRAL_PM_READ_HARD_CAP", 128)
v2_read_threads = env_int("CATHEDRAL_V2_READ_THREADS", 6)
v2_submit_bitset_threads = env_int("CATHEDRAL_V2_SUBMIT_BITSET_THREADS", 8)
v2_verify_batch_size = env_int("CATHEDRAL_V2_VERIFY_BATCH_SIZE", 8)
v2_bitset_verify_threads = env_int("CATHEDRAL_V2_BITSET_VERIFY_THREADS", 8)
submit_hard_cap = env_int("CATHEDRAL_SUBMIT_HARD_CAP", 8)
submit_max_concurrency = env_int("CATHEDRAL_SUBMIT_MAX_CONCURRENCY", 24)
v2_submit_backpressure_enabled = env_bool("CATHEDRAL_V2_SUBMIT_BACKPRESSURE_ENABLED", False)
v2_submit_backpressure_max_pending = env_int("CATHEDRAL_V2_SUBMIT_BACKPRESSURE_MAX_PENDING", 0)
v2_submit_backpressure_max_oldest_age_secs = env_float("CATHEDRAL_V2_SUBMIT_BACKPRESSURE_MAX_OLDEST_AGE_SECS", 0.0)
v2_real_fraction = env_float("CATHEDRAL_V2_REAL_FRACTION", 0.0)
statement_timeout_ms = env_int("CATHEDRAL_PG_STATEMENT_TIMEOUT_MS", 0)
weights_window_hours = float(weights_mod.window_hours())
max_pm_read_hard_cap = env_int("CATHEDRAL_PREFLIGHT_MAX_PM_READ_HARD_CAP", 8)
max_v2_read_threads = env_int("CATHEDRAL_PREFLIGHT_MAX_V2_READ_THREADS", 4)
max_v2_submit_bitset_threads = env_int("CATHEDRAL_PREFLIGHT_MAX_V2_SUBMIT_BITSET_THREADS", 4)
max_v2_verify_batch_size = env_int("CATHEDRAL_PREFLIGHT_MAX_V2_VERIFY_BATCH_SIZE", 8)
max_v2_bitset_verify_threads = env_int("CATHEDRAL_PREFLIGHT_MAX_V2_BITSET_VERIFY_THREADS", 1)
max_submit_hard_cap = env_int("CATHEDRAL_PREFLIGHT_MAX_SUBMIT_HARD_CAP", 32)
max_submit_max_concurrency = env_int("CATHEDRAL_PREFLIGHT_MAX_SUBMIT_MAX_CONCURRENCY", 32)
max_statement_timeout_ms = env_int("CATHEDRAL_PREFLIGHT_MAX_PG_STATEMENT_TIMEOUT_MS", 4000)
require_v2_submit_backpressure = env_bool("CATHEDRAL_PREFLIGHT_REQUIRE_V2_SUBMIT_BACKPRESSURE", False)
max_v2_submit_backpressure_pending = env_int("CATHEDRAL_PREFLIGHT_MAX_V2_SUBMIT_BACKPRESSURE_PENDING", 5000)
max_v2_submit_backpressure_oldest_age_secs = env_float("CATHEDRAL_PREFLIGHT_MAX_V2_SUBMIT_BACKPRESSURE_OLDEST_AGE_SECS", 300.0)
min_weights_window_hours = float(os.environ.get("CATHEDRAL_PREFLIGHT_MIN_WEIGHTS_WINDOW_HOURS", "48") or "48")
expected_v2_real_fraction_raw = os.environ.get("CATHEDRAL_PREFLIGHT_EXPECTED_V2_REAL_FRACTION", "").strip()
expected_v2_real_fraction = None
expected_v2_real_fraction_ok = True
if expected_v2_real_fraction_raw:
    try:
        expected_v2_real_fraction = float(expected_v2_real_fraction_raw)
    except ValueError:
        expected_v2_real_fraction_ok = False
    else:
        expected_v2_real_fraction_ok = abs(v2_real_fraction - expected_v2_real_fraction) <= 0.000000001
checks = {
    "pm_read_hard_cap_positive": pm_read_hard_cap > 0,
    "pm_read_hard_cap_within_launch_ceiling": pm_read_hard_cap <= max_pm_read_hard_cap,
    "v2_read_threads_within_launch_ceiling": 0 < v2_read_threads <= max_v2_read_threads,
    "v2_submit_bitset_threads_within_launch_ceiling": 0 < v2_submit_bitset_threads <= max_v2_submit_bitset_threads,
    "v2_verify_batch_size_within_launch_ceiling": 0 < v2_verify_batch_size <= max_v2_verify_batch_size,
    "v2_bitset_verify_threads_within_launch_ceiling": 0 < v2_bitset_verify_threads <= max_v2_bitset_verify_threads,
    "submit_hard_cap_within_launch_ceiling": 0 < submit_hard_cap <= max_submit_hard_cap,
    "submit_max_concurrency_within_launch_ceiling": 0 < submit_max_concurrency <= max_submit_max_concurrency,
    "v2_submit_backpressure_required": (
        (not require_v2_submit_backpressure) or v2_submit_backpressure_enabled
    ),
    "v2_submit_backpressure_pending_within_launch_ceiling": (
        (not v2_submit_backpressure_enabled)
        or (0 < v2_submit_backpressure_max_pending <= max_v2_submit_backpressure_pending)
    ),
    "v2_submit_backpressure_age_within_launch_ceiling": (
        (not v2_submit_backpressure_enabled)
        or (0 < v2_submit_backpressure_max_oldest_age_secs <= max_v2_submit_backpressure_oldest_age_secs)
    ),
    "v2_real_fraction_range": 0.0 <= v2_real_fraction <= 1.0,
    "v2_real_fraction_matches_expected": expected_v2_real_fraction_ok,
    "statement_timeout_positive": statement_timeout_ms > 0,
    "statement_timeout_within_launch_ceiling": statement_timeout_ms <= max_statement_timeout_ms,
    "weights_window_launch_bridge": weights_window_hours >= min_weights_window_hours,
}
payload = {
    "ok": all(checks.values()),
    "checks": checks,
    "pm_read_hard_cap": pm_read_hard_cap,
    "max_pm_read_hard_cap": max_pm_read_hard_cap,
    "v2_read_threads": v2_read_threads,
    "max_v2_read_threads": max_v2_read_threads,
    "v2_submit_bitset_threads": v2_submit_bitset_threads,
    "max_v2_submit_bitset_threads": max_v2_submit_bitset_threads,
    "v2_verify_batch_size": v2_verify_batch_size,
    "max_v2_verify_batch_size": max_v2_verify_batch_size,
    "v2_bitset_verify_threads": v2_bitset_verify_threads,
    "max_v2_bitset_verify_threads": max_v2_bitset_verify_threads,
    "submit_hard_cap": submit_hard_cap,
    "max_submit_hard_cap": max_submit_hard_cap,
    "submit_max_concurrency": submit_max_concurrency,
    "max_submit_max_concurrency": max_submit_max_concurrency,
    "v2_submit_backpressure_enabled": v2_submit_backpressure_enabled,
    "v2_submit_backpressure_max_pending": v2_submit_backpressure_max_pending,
    "max_v2_submit_backpressure_pending": max_v2_submit_backpressure_pending,
    "v2_submit_backpressure_max_oldest_age_secs": v2_submit_backpressure_max_oldest_age_secs,
    "max_v2_submit_backpressure_oldest_age_secs": max_v2_submit_backpressure_oldest_age_secs,
    "require_v2_submit_backpressure": require_v2_submit_backpressure,
    "v2_real_fraction": v2_real_fraction,
    "expected_v2_real_fraction": expected_v2_real_fraction,
    "expected_v2_real_fraction_raw": expected_v2_real_fraction_raw,
    "statement_timeout_ms": statement_timeout_ms,
    "max_statement_timeout_ms": max_statement_timeout_ms,
    "weights_window_hours": weights_window_hours,
    "min_weights_window_hours": min_weights_window_hours,
}
print(json.dumps(payload, sort_keys=True))
raise SystemExit(0 if payload["ok"] else 1)
PY"""
    )
    proc = subprocess.run(
        ssh_cmd + [target, remote_guardrails],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    try:
        payload = json.loads(proc.stdout.splitlines()[-1])
    except Exception as exc:
        pf.fail(
            "remote runtime guardrails",
            f"bad JSON: {exc}; output={proc.stdout[-500:].strip()}",
        )
    else:
        detail = (
            f"pm_read_cap={payload.get('pm_read_hard_cap')}"
            f"/<={payload.get('max_pm_read_hard_cap')} "
            f"v2_read_threads={payload.get('v2_read_threads')}"
            f"/<={payload.get('max_v2_read_threads')} "
            f"v2_submit_threads={payload.get('v2_submit_bitset_threads')}"
            f"/<={payload.get('max_v2_submit_bitset_threads')} "
            f"v2_verify_batch={payload.get('v2_verify_batch_size')}"
            f"/<={payload.get('max_v2_verify_batch_size')} "
            f"v2_bitset_verify_threads={payload.get('v2_bitset_verify_threads')}"
            f"/<={payload.get('max_v2_bitset_verify_threads')} "
            f"submit_hard_cap={payload.get('submit_hard_cap')}"
            f"/<={payload.get('max_submit_hard_cap')} "
            f"submit_max_concurrency={payload.get('submit_max_concurrency')}"
            f"/<={payload.get('max_submit_max_concurrency')} "
            f"v2_submit_backpressure={payload.get('v2_submit_backpressure_enabled')} "
            f"pending_cap={payload.get('v2_submit_backpressure_max_pending')}"
            f"/<={payload.get('max_v2_submit_backpressure_pending')} "
            f"oldest_age_cap={payload.get('v2_submit_backpressure_max_oldest_age_secs')}"
            f"/<={payload.get('max_v2_submit_backpressure_oldest_age_secs')} "
            f"v2_real_fraction={payload.get('v2_real_fraction')} "
            f"expected_v2_real_fraction={payload.get('expected_v2_real_fraction')} "
            f"stmt_timeout_ms={payload.get('statement_timeout_ms')}"
            f"/<={payload.get('max_statement_timeout_ms')} "
            f"window={payload.get('weights_window_hours')}h"
            f">={payload.get('min_weights_window_hours')}h"
        )
        if proc.returncode == 0 and payload.get("ok") is True:
            pf.pass_("remote runtime guardrails", detail)
        else:
            failed = [
                name for name, ok in (payload.get("checks") or {}).items() if not ok
            ]
            pf.fail(
                "remote runtime guardrails",
                f"{detail}; failed={failed}",
            )

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

    remote_retention = (
        f"cd {remote_dir} && set -a && . {remote_env} && set +a && "
        f"CATHEDRAL_PREFLIGHT_RETENTION_BATCH_SIZE={int(args.retention_batch_size)} "
        """.venv/bin/python - <<'PY'
import json
import os

from scaffold.publisher import retention
from scaffold.publisher import weights as weights_mod
from scaffold.publisher.store import Store


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


batch_size = env_int("CATHEDRAL_PREFLIGHT_RETENTION_BATCH_SIZE", 25000)
os.environ["CATHEDRAL_RETENTION_BATCH_SIZE"] = str(batch_size)
store = Store(os.environ.get("CATHEDRAL_DB_PATH", "cathedral.db"))
window_hours = float(weights_mod.window_hours())
solve_ledger_hours = int(retention.solve_ledger_hours())
eval_runs_hours = int(retention.eval_runs_hours())
pm_attempt_hours = int(retention.pm_attempt_hours())
summary = retention.retention_tick(store, dry=True)
deleted = summary.get("deleted") or {}
compacted = summary.get("compacted") or {}
counts_within_batch = all(int(value or 0) <= batch_size for value in deleted.values())
ok = (
    summary.get("dry_run") is True
    and solve_ledger_hours >= window_hours
    and eval_runs_hours >= window_hours
    and pm_attempt_hours >= window_hours
    and counts_within_batch
)
payload = {
    "ok": ok,
    "retention_enabled": retention.retention_enabled(),
    "retention_dry_run_env": retention.dry_run(),
    "window_hours": window_hours,
    "solve_ledger_hours": solve_ledger_hours,
    "eval_runs_hours": eval_runs_hours,
    "pm_attempt_hours": pm_attempt_hours,
    "batch_size": batch_size,
    "deleted": deleted,
    "compacted": compacted,
    "counts_within_batch": counts_within_batch,
}
print(json.dumps(payload, sort_keys=True))
raise SystemExit(0 if payload["ok"] else 1)
PY"""
    )
    proc = subprocess.run(
        ssh_cmd + [target, remote_retention],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
    )
    try:
        payload = json.loads(proc.stdout.splitlines()[-1])
    except Exception as exc:
        pf.fail(
            "remote retention dry-run",
            f"bad JSON: {exc}; output={proc.stdout[-500:].strip()}",
        )
    else:
        deleted = payload.get("deleted") or {}
        detail = (
            f"enabled={payload.get('retention_enabled')} "
            f"window={payload.get('window_hours')}h "
            f"solve_retention={payload.get('solve_ledger_hours')}h "
            f"batch={payload.get('batch_size')} "
            f"would_delete_per_miner_solves={deleted.get('per_miner_solves')} "
            f"would_delete_assignments={deleted.get('per_miner_assignments')}"
        )
        if proc.returncode == 0 and payload.get("ok") is True:
            pf.pass_("remote retention dry-run", detail)
        else:
            pf.fail(
                "remote retention dry-run",
                f"{detail}; counts={deleted} compacted={payload.get('compacted')}",
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

    remote_template = (
        f"cd {remote_dir} && "
        f".venv/bin/python deploy/check_env_template.py "
        f"--template {shlex.quote(args.ssh_env_template)} --env-file {remote_env}"
    )
    proc = subprocess.run(
        ssh_cmd + [target, remote_template],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    if proc.returncode == 0:
        pf.pass_("remote env template", "live env matches sandbox template")
    else:
        pf.fail("remote env template", proc.stdout[-1000:].strip())

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
        f"CATHEDRAL_PREFLIGHT_PREBAKE_EPOCH_LOOKAHEAD={int(args.prebake_epoch_lookahead)} "
        """.venv/bin/python - <<'PY'
import json
import os

from scaffold.publisher import per_miner as pm
from scaffold.publisher import v2_pipeline
from scaffold.publisher import weights as weights_mod
from scaffold.publisher.store import Store

depth = max(1, int(os.environ.get("CATHEDRAL_PREFLIGHT_PREBAKE_DEPTH", "10") or "10"))
epoch_lookahead = max(0, int(os.environ.get("CATHEDRAL_PREFLIGHT_PREBAKE_EPOCH_LOOKAHEAD", "0") or "0"))
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
current_epoch = pm.current_epoch()
epochs = list(range(current_epoch, current_epoch + epoch_lookahead + 1))
expected_by_epoch = {}
for epoch in epochs:
    expected_ids = []
    for identity in identities:
        for tier in pm.TIERS:
            for seq in range(min(depth, pm.allotment_for(tier))):
                expected_ids.append(pm.instance_id(identity, epoch, tier, seq))
    expected_by_epoch[epoch] = expected_ids

present = set()
all_expected_ids = [cid for ids in expected_by_epoch.values() for cid in ids]
for idx in range(0, len(all_expected_ids), 500):
    chunk = all_expected_ids[idx:idx + 500]
    if not chunk:
        continue
    placeholders = ",".join("?" for _ in chunk)
    q = f"SELECT challenge_id FROM v2_cnf_store WHERE challenge_id IN ({placeholders})"
    for row in store.query(q, tuple(chunk)):
        present.add(str(row["challenge_id"]))

epochs_payload = []
missing = []
for epoch, expected_ids in expected_by_epoch.items():
    epoch_missing = [cid for cid in expected_ids if cid not in present]
    missing.extend(epoch_missing)
    epochs_payload.append({
        "epoch": epoch,
        "expected": len(expected_ids),
        "present": len(expected_ids) - len(epoch_missing),
        "missing": len(epoch_missing),
        "missing_samples": epoch_missing[:5],
    })
payload = {
    "ok": bool(all_expected_ids) and not missing,
    "store_backend": store.backend,
    "store_source": store_source,
    "current_epoch": current_epoch,
    "epoch_lookahead": epoch_lookahead,
    "epochs": epochs_payload,
    "depth": depth,
    "hotkeys": len(hotkeys),
    "identities": len(identities),
    "expected": len(all_expected_ids),
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
        epochs = payload.get("epochs") or []
        epoch_detail = ",".join(
            f"{item.get('epoch')}:{item.get('present')}/{item.get('expected')}"
            for item in epochs
        )
        detail = (
            f"epochs={epoch_detail} depth={payload.get('depth')} "
            f"lookahead={payload.get('epoch_lookahead')} "
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


def check_capacity_probe(pf: Preflight, args: argparse.Namespace) -> None:
    run_probe = args.run_capacity_probe or os.environ.get(
        "CATHEDRAL_PREFLIGHT_RUN_CAPACITY_PROBE", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not run_probe:
        pf.warn("V2 capacity probe", "skipped; set --run-capacity-probe for submit/drain burst")
        return
    base = (args.capacity_base or args.base).rstrip("/")
    challenge_base = (args.capacity_challenge_base or base).rstrip("/")
    submit_base = (args.capacity_submit_base or base).rstrip("/")
    metrics_base = (args.capacity_metrics_base or submit_base).rstrip("/")
    run(
        pf,
        "V2 capacity probe",
        [
            select_python(),
            "scripts/v2_bitset_capacity_probe.py",
            "--base",
            base,
            "--challenge-base",
            challenge_base,
            "--submit-base",
            submit_base,
            "--metrics-base",
            metrics_base,
            "--miners",
            str(args.capacity_miners),
            "--per-miner-limit",
            str(args.capacity_per_miner_limit),
            "--submit-concurrency",
            str(args.capacity_submit_concurrency),
            "--max-drain-secs",
            str(args.capacity_max_drain_secs),
            "--max-admit-p95-ms",
            str(args.capacity_max_admit_p95_ms),
            "--min-drain-rate-per-sec",
            str(args.capacity_min_drain_rate_per_sec),
            "--uri-prefix",
            args.capacity_uri_prefix,
        ],
        timeout=args.capacity_timeout_secs,
        check_contains="CAPACITY_OK",
    )


def check_edge_soak(pf: Preflight, args: argparse.Namespace) -> None:
    run_soak = args.run_edge_soak or os.environ.get(
        "CATHEDRAL_PREFLIGHT_RUN_EDGE_SOAK", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not run_soak:
        pf.warn("staged edge soak", "skipped; set --run-edge-soak for edge stampede guard")
        return
    run(
        pf,
        "staged edge soak",
        [
            select_python(),
            "scripts/v2_edge_staged_soak.py",
            "--base",
            args.base.rstrip("/"),
            "--requests",
            str(args.edge_soak_requests),
            "--concurrency",
            str(args.edge_soak_concurrency),
            "--max-p95-ms",
            str(args.edge_soak_max_p95_ms),
            "--uri-prefix",
            args.edge_soak_uri_prefix,
        ],
        timeout=args.edge_soak_timeout_secs,
        check_contains="EDGE_STAGED_OK",
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=os.environ.get("CATHEDRAL_PREFLIGHT_BASE", DEFAULT_BASE))
    ap.add_argument("--weights-url", default=os.environ.get("CATHEDRAL_PREFLIGHT_WEIGHTS_URL", DEFAULT_WEIGHTS_URL))
    ap.add_argument(
        "--launch-intent",
        choices=("staged", "all-miner-open"),
        default=os.environ.get("CATHEDRAL_PREFLIGHT_LAUNCH_INTENT", "staged"),
    )
    ap.add_argument(
        "--expected-v2-real-fraction",
        default=os.environ.get("CATHEDRAL_PREFLIGHT_EXPECTED_V2_REAL_FRACTION", ""),
        help="Require the remote runtime CATHEDRAL_V2_REAL_FRACTION to match this exact launch decision.",
    )
    ap.add_argument("--env-file", default=os.environ.get("CATHEDRAL_PREFLIGHT_ENV_FILE", ""))
    ap.add_argument("--env-template", default=os.environ.get("CATHEDRAL_PREFLIGHT_ENV_TEMPLATE", "deploy/sandbox/env.template.sh"))
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
    ap.add_argument(
        "--ssh-env-template",
        default=os.environ.get(
            "CATHEDRAL_PREFLIGHT_SSH_ENV_TEMPLATE",
            "deploy/sandbox/env.template.sh",
        ),
    )
    ap.add_argument("--max-pending", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_MAX_PENDING", "0") or "0"))
    ap.add_argument("--max-oldest-pending-age-secs", type=float, default=float(os.environ.get("CATHEDRAL_PREFLIGHT_MAX_OLDEST_PENDING_AGE_SECS", "120") or "120"))
    ap.add_argument("--max-weight-age-secs", type=float, default=float(os.environ.get("CATHEDRAL_PREFLIGHT_MAX_WEIGHT_AGE_SECS", "900") or "900"))
    ap.add_argument("--min-weight-count", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_MIN_WEIGHT_COUNT", "300") or "300"))
    ap.add_argument("--min-prebake-depth", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_MIN_PREBAKE_DEPTH", "10") or "10"))
    ap.add_argument("--prebake-epoch-lookahead", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_PREBAKE_EPOCH_LOOKAHEAD", "0") or "0"))
    ap.add_argument("--pm-coverage-horizon-hours", type=float, default=float(os.environ.get("CATHEDRAL_PREFLIGHT_PM_COVERAGE_HORIZON_HOURS", "18") or "18"))
    ap.add_argument("--min-live-coverage-ratio", type=float, default=float(os.environ.get("CATHEDRAL_PREFLIGHT_MIN_LIVE_COVERAGE_RATIO", "0.85") or "0.85"))
    ap.add_argument("--max-pm-read-hard-cap", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_MAX_PM_READ_HARD_CAP", "8") or "8"))
    ap.add_argument("--max-v2-read-threads", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_MAX_V2_READ_THREADS", "4") or "4"))
    ap.add_argument("--max-v2-submit-bitset-threads", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_MAX_V2_SUBMIT_BITSET_THREADS", "4") or "4"))
    ap.add_argument("--max-v2-verify-batch-size", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_MAX_V2_VERIFY_BATCH_SIZE", "8") or "8"))
    ap.add_argument("--max-v2-bitset-verify-threads", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_MAX_V2_BITSET_VERIFY_THREADS", "1") or "1"))
    ap.add_argument("--max-submit-hard-cap", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_MAX_SUBMIT_HARD_CAP", "32") or "32"))
    ap.add_argument("--max-submit-max-concurrency", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_MAX_SUBMIT_MAX_CONCURRENCY", "32") or "32"))
    ap.add_argument("--max-v2-submit-backpressure-pending", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_MAX_V2_SUBMIT_BACKPRESSURE_PENDING", "5000") or "5000"))
    ap.add_argument("--max-v2-submit-backpressure-oldest-age-secs", type=float, default=float(os.environ.get("CATHEDRAL_PREFLIGHT_MAX_V2_SUBMIT_BACKPRESSURE_OLDEST_AGE_SECS", "300") or "300"))
    ap.add_argument("--max-pg-statement-timeout-ms", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_MAX_PG_STATEMENT_TIMEOUT_MS", "4000") or "4000"))
    ap.add_argument("--min-weights-window-hours", type=float, default=float(os.environ.get("CATHEDRAL_PREFLIGHT_MIN_WEIGHTS_WINDOW_HOURS", "48") or "48"))
    ap.add_argument("--retention-batch-size", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_RETENTION_BATCH_SIZE", "25000") or "25000"))
    ap.add_argument("--min-policy-version", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_MIN_POLICY_VERSION", "1700000000000") or "1700000000000"))
    ap.add_argument("--pytest-timeout-secs", type=int, default=120)
    ap.add_argument("--e2e-timeout-secs", type=int, default=120)
    ap.add_argument("--e2e-uri", default=os.environ.get("CATHEDRAL_PREFLIGHT_E2E_URI", "//Alice"))
    ap.add_argument("--run-capacity-probe", action="store_true")
    ap.add_argument("--capacity-base", default=os.environ.get("CATHEDRAL_PREFLIGHT_CAPACITY_BASE", ""))
    ap.add_argument("--capacity-challenge-base", default=os.environ.get("CATHEDRAL_PREFLIGHT_CAPACITY_CHALLENGE_BASE", ""))
    ap.add_argument("--capacity-submit-base", default=os.environ.get("CATHEDRAL_PREFLIGHT_CAPACITY_SUBMIT_BASE", ""))
    ap.add_argument("--capacity-metrics-base", default=os.environ.get("CATHEDRAL_PREFLIGHT_CAPACITY_METRICS_BASE", ""))
    ap.add_argument("--capacity-miners", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_CAPACITY_MINERS", "4") or "4"))
    ap.add_argument("--capacity-per-miner-limit", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_CAPACITY_PER_MINER_LIMIT", "4") or "4"))
    ap.add_argument("--capacity-submit-concurrency", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_CAPACITY_SUBMIT_CONCURRENCY", "8") or "8"))
    ap.add_argument("--capacity-max-drain-secs", type=float, default=float(os.environ.get("CATHEDRAL_PREFLIGHT_CAPACITY_MAX_DRAIN_SECS", "20") or "20"))
    ap.add_argument("--capacity-max-admit-p95-ms", type=float, default=float(os.environ.get("CATHEDRAL_PREFLIGHT_CAPACITY_MAX_ADMIT_P95_MS", "1000") or "1000"))
    ap.add_argument("--capacity-min-drain-rate-per-sec", type=float, default=float(os.environ.get("CATHEDRAL_PREFLIGHT_CAPACITY_MIN_DRAIN_RATE_PER_SEC", "0") or "0"))
    ap.add_argument("--capacity-uri-prefix", default=os.environ.get("CATHEDRAL_PREFLIGHT_CAPACITY_URI_PREFIX", "//CapacityProbe"))
    ap.add_argument("--capacity-timeout-secs", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_CAPACITY_TIMEOUT_SECS", "180") or "180"))
    ap.add_argument("--run-edge-soak", action="store_true")
    ap.add_argument("--edge-soak-requests", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_EDGE_SOAK_REQUESTS", "64") or "64"))
    ap.add_argument("--edge-soak-concurrency", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_EDGE_SOAK_CONCURRENCY", "16") or "16"))
    ap.add_argument("--edge-soak-max-p95-ms", type=float, default=float(os.environ.get("CATHEDRAL_PREFLIGHT_EDGE_SOAK_MAX_P95_MS", "1500") or "1500"))
    ap.add_argument("--edge-soak-uri-prefix", default=os.environ.get("CATHEDRAL_PREFLIGHT_EDGE_SOAK_URI_PREFIX", "//EdgeStagedSoak"))
    ap.add_argument("--edge-soak-timeout-secs", type=int, default=int(os.environ.get("CATHEDRAL_PREFLIGHT_EDGE_SOAK_TIMEOUT_SECS", "120") or "120"))
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
    check_capacity_probe(pf, args)
    check_edge_soak(pf, args)

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
