"""`cathedral-validator` — the console entry point.

v4 keeps the command surface operators already know — `cathedral-validator
migrate` then `cathedral-validator serve` — so updating from a prior release is
the same muscle memory and the same systemd unit. Underneath, `serve` runs the
v4 thin validator (fetch one signed score per miner, verify, apply): no local
database, no rolling window. `migrate` is therefore a no-op kept only so
existing update scripts don't break.

Config resolution for `serve`, lowest to highest precedence:
  built-in defaults  <  --config TOML  <  environment  <  command-line flags
A sample config ships at `config/validator.toml`.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from . import validator_thin

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


_DEFAULTS = {
    "publisher_url": "https://api.cathedral.computer",
    "public_key_hex": "",
    "key_id": "cathedral-weight-policy",
    "network": "finney",
    "netuid": 39,
    "wallet_name": "validator",
    "wallet_hotkey": "default",
    "state_file": str(Path.home() / ".cathedral" / "thin_validator.json"),
    "interval_secs": 1500.0,
    "once": False,
    "offline": False,         # set by --offline (verify+print, no chain access)
    "broadcast": True,        # `serve` is a live validator by default (legacy parity)
}

# config-file keys -> our flat config keys (a [section].key map, flattened)
_CONFIG_MAP = {
    ("network", "name"): "network",
    ("network", "netuid"): "netuid",
    ("network", "wallet_name"): "wallet_name",
    ("network", "validator_hotkey"): "wallet_hotkey",
    ("publisher", "url"): "publisher_url",
    ("weight_policy", "public_key_hex"): "public_key_hex",
    ("weight_policy", "key_id"): "key_id",
    ("weight_policy", "state_file"): "state_file",
    ("weights", "interval_secs"): "interval_secs",
}

# env var -> our flat config key
_ENV_MAP = {
    "CATHEDRAL_PUBLISHER_URL": "publisher_url",
    "CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY": "public_key_hex",
    "CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX": "public_key_hex",  # legacy spelling
    "CATHEDRAL_WEIGHT_POLICY_KEY_ID": "key_id",
    "BT_WALLET_NAME": "wallet_name",
    "BT_WALLET_HOTKEY": "wallet_hotkey",
    "CATHEDRAL_VALIDATOR_STATE": "state_file",
}


def _load_config_file(path: str) -> dict:
    if tomllib is None:
        raise RuntimeError("TOML config requires Python 3.11+")
    with open(path, "rb") as fh:
        doc = tomllib.load(fh)
    out: dict = {}
    for (section, key), flat in _CONFIG_MAP.items():
        if section in doc and key in doc[section]:
            out[flat] = doc[section][key]
    return out


def _resolve_serve_config(ns: argparse.Namespace) -> SimpleNamespace:
    cfg = dict(_DEFAULTS)
    if ns.config:
        cfg.update(_load_config_file(ns.config))
    for env, flat in _ENV_MAP.items():
        v = os.environ.get(env)
        if v:
            cfg[flat] = v
    # explicit flags win
    for flat in ("publisher_url", "public_key_hex", "network", "netuid",
                 "wallet_name", "wallet_hotkey", "state_file", "interval_secs"):
        v = getattr(ns, flat, None)
        if v is not None:
            cfg[flat] = v
    if ns.dry_run:
        cfg["broadcast"] = False
    if ns.once:
        cfg["once"] = True
    if getattr(ns, "offline", False):
        cfg["offline"] = True
        cfg["broadcast"] = False
    cfg["netuid"] = int(cfg["netuid"])
    cfg["interval_secs"] = float(cfg["interval_secs"])
    return SimpleNamespace(**cfg)


def _cmd_serve(ns: argparse.Namespace) -> int:
    cfg = _resolve_serve_config(ns)
    if not cfg.public_key_hex:
        print("error: no signing key pinned. Set [weight_policy].public_key_hex in your "
              "config, or CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY, or --public-key-hex.\n"
              "It is the key published at "
              "https://api.cathedral.computer/.well-known/cathedral-jwks.json", file=sys.stderr)
        return 2
    mode = "BROADCAST (setting weights)" if cfg.broadcast else "DRY-RUN (no chain writes)"
    print(f"cathedral-validator serve — netuid {cfg.netuid} on {cfg.network} — {mode}")
    print(f"  publisher={cfg.publisher_url}  key_id={cfg.key_id}  pinned={cfg.public_key_hex[:16]}…")
    return validator_thin.run(cfg)


def _cmd_migrate(ns: argparse.Namespace) -> int:
    print("nothing to migrate — v4 keeps no local validator database "
          "(scoring is composed and signed by the orchestrator).")
    return 0


def _cmd_version(ns: argparse.Namespace) -> int:
    from . import __version__
    print(f"cathedral-validator {__version__}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="cathedral-validator",
        description="Cathedral SN39 validator (v4) — fetch one signed score per miner, "
                    "verify, apply.")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("serve", help="run the validator (live by default; --dry-run to test)")
    sp.add_argument("--config", default=os.environ.get("CATHEDRAL_VALIDATOR_CONFIG"),
                    help="path to a TOML config (e.g. config/validator.toml)")
    sp.add_argument("--publisher-url", dest="publisher_url", default=None)
    sp.add_argument("--public-key-hex", dest="public_key_hex", default=None)
    sp.add_argument("--network", dest="network", default=None)
    sp.add_argument("--netuid", dest="netuid", type=int, default=None)
    sp.add_argument("--wallet-name", dest="wallet_name", default=None)
    sp.add_argument("--wallet-hotkey", dest="wallet_hotkey", default=None)
    sp.add_argument("--state-file", dest="state_file", default=None)
    sp.add_argument("--interval-secs", dest="interval_secs", type=float, default=None)
    sp.add_argument("--dry-run", action="store_true",
                    help="verify and print the weights without setting them on chain")
    sp.add_argument("--offline", action="store_true",
                    help="verify + print only, no chain access (CI / smoke)")
    sp.add_argument("--once", action="store_true", help="single tick then exit")
    sp.set_defaults(func=_cmd_serve)

    mp = sub.add_parser("migrate", help="(no-op in v4 — kept for update-script parity)")
    mp.set_defaults(func=_cmd_migrate)

    vp = sub.add_parser("version", help="print version")
    vp.set_defaults(func=_cmd_version)

    ns = p.parse_args(argv)
    return ns.func(ns)


if __name__ == "__main__":
    raise SystemExit(main())
