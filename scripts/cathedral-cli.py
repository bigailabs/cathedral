#!/usr/bin/env python3
"""
cathedral-cli — quick command line for listing machines and running rentals
on cathedral (Bittensor SN39).

Usage:
    cathedral-cli list                            # show all available machines
    cathedral-cli list --category RTX_5090        # filter by GPU category
    cathedral-cli list --max-price-cents 50       # under $0.50/GPU/hr
    cathedral-cli rent <node_id> --ssh-key ~/.ssh/id_ed25519.pub --max-cents 45
    cathedral-cli status <rental_id>
    cathedral-cli terminate <rental_id>
    cathedral-cli rentals                         # list all my rentals

Env:
    CATHEDRAL_API             default https://api.polaris.computer
    CATHEDRAL_API_KEY         optional; sent as X-Polaris-API-Key
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

DEFAULT_API = os.getenv("CATHEDRAL_API", "https://api.polaris.computer")
API_KEY = os.getenv("CATHEDRAL_API_KEY", "")


def _req(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    url = f"{DEFAULT_API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if API_KEY:
        req.add_header("X-Polaris-API-Key", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {"error": str(e)}


def _fmt_price(cents: int | None) -> str:
    if cents is None:
        return "-"
    return f"${cents/100:.2f}/hr"


def cmd_list(args: argparse.Namespace) -> int:
    qs = []
    if args.category:
        qs.append(f"category={args.category}")
    if args.min_memory_gb is not None:
        qs.append(f"min_memory_gb={args.min_memory_gb}")
    if args.max_price_cents is not None:
        qs.append(f"max_price_cents={args.max_price_cents}")
    path = "/api/v1/cathedral/machines"
    if qs:
        path += "?" + "&".join(qs)
    status, body = _req("GET", path)
    if status >= 400:
        print(f"error: {status} {body}", file=sys.stderr)
        return 1

    machines = body.get("machines", [])
    if not machines:
        print("(no machines match your filters)")
        return 0

    if args.json:
        print(json.dumps(machines, indent=2))
        return 0

    # Human table
    cols = ("node_id", "category", "count", "mem(gb)", "location", "price", "net_down")
    widths = (12, 28, 6, 8, 22, 10, 10)
    print(" ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print(" ".join("-" * w for w in widths))
    for m in machines:
        row = (
            str(m.get("node_id") or "")[:11],
            str(m.get("category") or "")[:27],
            str(m.get("gpu_count") if not m.get("is_cpu_only") else m.get("cpu_cores") or ""),
            str(m.get("memory_gb") or ""),
            str(m.get("location") or "")[:21],
            _fmt_price(m.get("hourly_rate_cents")),
            f"{m.get('network_mbps_down') or 0:.0f}mbps",
        )
        print(" ".join(v.ljust(w) for v, w in zip(row, widths)))
    print(f"\n{body.get('total', 0)} machine(s)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    status, body = _req("GET", f"/api/v1/cathedral/machines/{args.node_id}")
    if status >= 400:
        print(f"error: {status} {body}", file=sys.stderr)
        return 1
    print(json.dumps(body, indent=2))
    return 0


def cmd_rent(args: argparse.Namespace) -> int:
    # Read public key from file or inline
    key_arg = args.ssh_key
    if key_arg and Path(key_arg).is_file():
        ssh_pub = Path(key_arg).read_text().strip()
    else:
        ssh_pub = key_arg.strip() if key_arg else ""
    if not ssh_pub.startswith("ssh-"):
        print("error: --ssh-key must be a path to a public key or an inline key starting with 'ssh-'", file=sys.stderr)
        return 2

    payload = {
        "gpu_category": args.category,
        "gpu_count": args.gpu_count,
        "max_hourly_rate_cents": args.max_cents,
        "ssh_public_key": ssh_pub,
        "container_image": args.image,
        "command": ["sleep", "infinity"] if not args.command else args.command,
    }
    if args.min_memory_gb is not None:
        payload["min_memory_gb"] = args.min_memory_gb

    status, body = _req("POST", "/api/v1/cathedral/rentals", payload)
    if status >= 400:
        print(f"error: {status} {body}", file=sys.stderr)
        return 1
    print(json.dumps(body, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    status, body = _req("GET", f"/api/v1/cathedral/rentals/{args.rental_id}")
    if status >= 400:
        print(f"error: {status} {body}", file=sys.stderr)
        return 1
    print(json.dumps(body, indent=2))
    return 0


def cmd_terminate(args: argparse.Namespace) -> int:
    status, body = _req("DELETE", f"/api/v1/cathedral/rentals/{args.rental_id}")
    if status >= 400:
        print(f"error: {status} {body}", file=sys.stderr)
        return 1
    print(json.dumps(body, indent=2))
    return 0


def cmd_rentals(_args: argparse.Namespace) -> int:
    status, body = _req("GET", "/api/v1/cathedral/rentals")
    if status >= 400:
        print(f"error: {status} {body}", file=sys.stderr)
        return 1
    print(json.dumps(body, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="cathedral-cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list available machines")
    p_list.add_argument("--category", help="GPU category filter (e.g. RTX_5090)")
    p_list.add_argument("--min-memory-gb", type=int, dest="min_memory_gb")
    p_list.add_argument("--max-price-cents", type=int, dest="max_price_cents")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="show a single machine")
    p_show.add_argument("node_id")
    p_show.set_defaults(func=cmd_show)

    p_rent = sub.add_parser("rent", help="start a rental")
    p_rent.add_argument("--category", required=True)
    p_rent.add_argument("--gpu-count", type=int, default=1, dest="gpu_count")
    p_rent.add_argument("--max-cents", type=int, required=True, dest="max_cents")
    p_rent.add_argument("--ssh-key", required=True, dest="ssh_key",
                        help="path to public key, or inline 'ssh-ed25519 ...' string")
    p_rent.add_argument("--image", default="ubuntu:24.04")
    p_rent.add_argument("--command", nargs="*")
    p_rent.add_argument("--min-memory-gb", type=int, dest="min_memory_gb")
    p_rent.set_defaults(func=cmd_rent)

    p_st = sub.add_parser("status", help="rental status")
    p_st.add_argument("rental_id")
    p_st.set_defaults(func=cmd_status)

    p_term = sub.add_parser("terminate", help="terminate a rental")
    p_term.add_argument("rental_id")
    p_term.set_defaults(func=cmd_terminate)

    p_ren = sub.add_parser("rentals", help="list my rentals")
    p_ren.set_defaults(func=cmd_rentals)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
