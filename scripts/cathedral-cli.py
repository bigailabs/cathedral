#!/usr/bin/env python3
"""
cathedral-cli — operator tooling for Cathedral (Bittensor SN39).

Scope: miner and validator operations only. Cathedral is the subnet;
user-facing compute rental lives on polaris.computer (polaris-cli).

Usage:
    cathedral-cli status                 # overall subnet state
    cathedral-cli miner <uid>            # one miner's validator-side state
    cathedral-cli miners                 # all registered-with-us miners
    cathedral-cli validators             # 14 permit holders on SN39

Env:
    CATHEDRAL_API    default https://api.polaris.computer
                     (validator APIs are proxied through polaris infra; this
                     is the read surface the site uses, safe + public)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error

DEFAULT_API = os.getenv("CATHEDRAL_API", "https://api.polaris.computer")


def _get(path: str) -> tuple[int, dict]:
    url = f"{DEFAULT_API}{path}"
    req = urllib.request.Request(url)
    # Cloudflare bot-challenges the default Python-urllib UA. Any real UA
    # bypasses the challenge and reaches our backend.
    req.add_header("User-Agent", "cathedral-cli/0.2")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {"error": str(e)}


def _fmt_stake(x: float | None) -> str:
    if x is None:
        return "—"
    if x >= 1000:
        return f"{x:,.0f}"
    if x >= 10:
        return f"{x:.1f}"
    return f"{x:.2f}"


def cmd_status(_args: argparse.Namespace) -> int:
    status, chain = _get("/api/substrate/state")
    if status >= 400:
        print(f"error: {status} {chain}", file=sys.stderr)
        return 1
    summary = chain.get("summary") or {}

    status2, reg = _get("/api/substrate/validator/registry")
    reg_data = reg.get("data") or {} if status2 < 400 else {}

    print(f"Subnet:           SN39 · finney")
    print(f"Block:            {summary.get('block', '—'):,}")
    print(f"Our validator:    uid 123 ({(chain.get('miners') or [{}])[0].get('status', '—').lower() if False else 'see below'})")
    us = next((m for m in chain.get("miners", []) if m.get("uid") == 123), None)
    if us:
        print(f"  status:         {str(us.get('status','')).lower()}")
        print(f"  stake:          {_fmt_stake(us.get('stake'))}α")
        print(f"  incentive:      {_fmt_stake(us.get('incentive'))}")
    print()
    print(f"Miners declared:  {summary.get('active_miners', '—')} of {summary.get('total_miners', '—')}")
    print(f"Registered:       {reg_data.get('miners_with_nodes', 0)}")
    print(f"Nodes total:      {reg_data.get('nodes_registered', 0)}")
    print(f"Verified nodes:   {reg_data.get('nodes_verified', 0)}")
    return 0


def cmd_miners(_args: argparse.Namespace) -> int:
    status, reg = _get("/api/substrate/validator/registry")
    if status >= 400:
        print(f"error: {status} {reg}", file=sys.stderr)
        return 1
    miners = ((reg.get("data") or {}).get("miners")) or []
    if not miners:
        print("(no miners registered with our validator yet)")
        return 0
    # Fetch chain state for hotkey lookups
    _, chain = _get("/api/substrate/state")
    by_uid = {m["uid"]: m for m in (chain.get("miners") or []) if "uid" in m}

    widths = (6, 24, 12, 12)
    print("uid".ljust(widths[0]), "hotkey".ljust(widths[1]), "status".ljust(widths[2]), "bid".ljust(widths[3]))
    print("-" * sum(widths))
    order = {"verified": 0, "online": 1, "offline": 2}
    miners.sort(key=lambda m: (order.get(m.get("status"), 9), m.get("uid", 0)))
    for m in miners:
        chain_row = by_uid.get(m["uid"], {})
        hotkey = chain_row.get("hotkey", "")
        abbrev = (hotkey[:6] + "…" + hotkey[-4:]) if len(hotkey) > 12 else hotkey
        bid = "active" if m.get("bid_active") else "inactive"
        print(str(m["uid"]).ljust(widths[0]), abbrev.ljust(widths[1]), str(m.get("status","")).ljust(widths[2]), bid.ljust(widths[3]))
    return 0


def cmd_miner(args: argparse.Namespace) -> int:
    uid = args.uid
    status, reg = _get("/api/substrate/validator/registry")
    if status >= 400:
        print(f"error: {status} {reg}", file=sys.stderr)
        return 1
    miners = ((reg.get("data") or {}).get("miners")) or []
    m = next((x for x in miners if x.get("uid") == uid), None)

    _, chain = _get("/api/substrate/state")
    chain_row = next((x for x in (chain.get("miners") or []) if x.get("uid") == uid), None)

    print(f"UID:              {uid}")
    if chain_row:
        print(f"Hotkey:           {chain_row.get('hotkey', '—')}")
        print(f"Chain status:     {str(chain_row.get('status','')).lower()}")
        print(f"Chain axon:       {chain_row.get('ip', '—')}:{chain_row.get('port', '—')}")
        print(f"Stake:            {_fmt_stake(chain_row.get('stake'))}α")
    else:
        print("Chain status:     not on chain")
    print()
    if m:
        print(f"Validator status: {m.get('status')}")
        print(f"Bid active:       {bool(m.get('bid_active'))}")
        print(f"Active rental:    {bool(m.get('has_rental'))}")
    else:
        print("Validator status: not registered with our validator")
    return 0


def cmd_validators(_args: argparse.Namespace) -> int:
    status, chain = _get("/api/substrate/state")
    if status >= 400:
        print(f"error: {status} {chain}", file=sys.stderr)
        return 1
    miners = chain.get("miners") or []
    ranked = sorted(miners, key=lambda m: m.get("stake", 0) or 0, reverse=True)[:14]
    print(f"{'rank':<6}{'uid':<6}{'hotkey':<24}{'stake':>12}")
    print("-" * 48)
    for i, m in enumerate(ranked, 1):
        hk = m.get("hotkey", "")
        abbrev = (hk[:6] + "…" + hk[-4:]) if len(hk) > 12 else hk
        print(f"{i:<6}{m.get('uid',''):<6}{abbrev:<24}{_fmt_stake(m.get('stake')):>12}α")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="cathedral-cli",
        description="Operator tooling for Cathedral (Bittensor SN39). For compute rental / agent deploy, use polaris-cli.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="subnet state overview")
    p_status.set_defaults(func=cmd_status)

    p_miners = sub.add_parser("miners", help="list miners registered with our validator")
    p_miners.set_defaults(func=cmd_miners)

    p_miner = sub.add_parser("miner", help="one miner's state on cathedral")
    p_miner.add_argument("uid", type=int)
    p_miner.set_defaults(func=cmd_miner)

    p_val = sub.add_parser("validators", help="top 14 permit holders on SN39")
    p_val.set_defaults(func=cmd_validators)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
