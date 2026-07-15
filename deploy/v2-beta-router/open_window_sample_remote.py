#!/usr/bin/env python3
"""One open-window watch sample, run ON the sandbox host.

Prints a single line: local readiness, origin FD count, accept-queue
depth on :8080, verifier drain metrics, and shed counters. A full accept
queue (acceptq at its max) means the event loop has wedged and miners are
getting timeouts, even if nothing is erroring. Invoked by open_window_watch.sh over SSH.
Expects CATHEDRAL_PUBLISHER_ADMIN_TOKEN in the environment (source .env.sh)
and WATCH_FDS optionally pre-computed by the caller.
"""
import json
import os
import subprocess
import urllib.error
import urllib.request


def get(url: str, token: str | None = None, timeout: int = 5):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b"{}"
    except Exception:
        return 0, b"{}"


def origin_fds() -> str:
    pre = os.environ.get("WATCH_FDS", "").strip()
    if pre:
        return pre
    try:
        pid = subprocess.run(
            ["systemctl", "--user", "show", "cathedral-v2-beta-origin.service",
             "-p", "MainPID", "--value"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return str(len(os.listdir(f"/proc/{pid}/fd")))
    except Exception:
        return "?"


def accept_queue() -> str:
    """Accept-queue depth/backlog for the :8080 listener (Recv-Q/Send-Q)."""
    try:
        fields = subprocess.run(
            ["ss", "-ltnH", "sport = :8080"],
            capture_output=True, text=True, timeout=5,
        ).stdout.split()
        if len(fields) >= 3:
            return f"{fields[1]}/{fields[2]}"
    except Exception:
        pass
    return "?"


def main() -> None:
    ready, _ = get("http://127.0.0.1:8080/health/ready")
    _, vm_raw = get("http://127.0.0.1:8000/v2/verify/metrics")
    _, sm_raw = get(
        "http://127.0.0.1:8080/v1/admin/synthetic-boolean/submit-metrics",
        token=os.environ.get("CATHEDRAL_PUBLISHER_ADMIN_TOKEN", ""),
    )
    try:
        vm = json.loads(vm_raw)
    except Exception:
        vm = {}
    try:
        sm = json.loads(sm_raw)
    except Exception:
        sm = {}
    reasons = sm.get("by_reason") or {}
    print(
        f"local_ready={ready} fds={origin_fds()} acceptq={accept_queue()}"
        f" pending={vm.get('pending_count')}"
        f" oldest={vm.get('oldest_pending_age_secs')}"
        f" proc60={vm.get('processed_last_60s')}"
        f" ver60={vm.get('verified_last_60s')}"
        f" rej60={vm.get('rejected_last_60s')}"
        f" tickerr={vm.get('tick_errors_last_60s')}"
        f" shed[db={reasons.get('v2_db_unavailable_retry', 0)}"
        f" rcpt={reasons.get('receipt_poll_busy_retry', 0)}"
        f" bp={reasons.get('v2_submit_backpressure', 0)}"
        f" busy={reasons.get('submit_busy_retry', 0)}]"
    )


if __name__ == "__main__":
    main()
