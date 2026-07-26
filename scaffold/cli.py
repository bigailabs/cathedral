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
    # Shared by thin and FULL-authority processes, independent of HOME and
    # lane-specific state files. The service installer creates it mode 0700.
    "runtime_root": "/var/lib/cathedral-validator",
    "interval_secs": 1500.0,
    "max_submissions": 0,
    "require_full_provenance_for_broadcast": False,
    "require_completed_launch_for_broadcast": True,
    "launch_approval_file": str(validator_thin.SN39_LAUNCH_APPROVAL_FILE),
    "launch_release_sha": os.environ.get(validator_thin.SN39_RELEASE_SHA_ENV, ""),
    "launch_config_sha256": os.environ.get(
        validator_thin.SN39_LAUNCH_CONFIG_DIGEST_ENV, ""
    ),
    "launch_preflight": False,
    "once": False,
    "offline": False,  # set by --offline (verify+print, no chain access)
    "broadcast": False,  # every chain write requires an explicit --broadcast
    # Supported SN39 operation is PINNED to the launch policy contract;
    # operators must explicitly override to run unpinned (unsupported).
    "require_policy": "validated_supply_v1",
    # Two-mode operation: thin submits by default while the full-provenance
    # audit runs concurrently in shadow. "authority" submits the independent
    # recomputation instead; "off" disables the audit.
    "provenance": "shadow",
    "evidence_url": None,  # default: <publisher_url>/v1/evidence
    "evidence_dir": None,
    "provenance_registry_keys": None,
    "provenance_registry_keys_digest": None,
    "provenance_report_keys": None,
    "provenance_report_keys_digest": None,
    "provenance_index_keys": None,
    "provenance_index_keys_digest": None,
    "provenance_verifier_digest": None,
    "provenance_mechanism": "validated_supply_v1",
    "provenance_controlled_dir": None,
    "provenance_verifier_binary": None,
    "provenance_source_revision": None,
    "provenance_burn_hotkey": None,
    "provenance_index_max_age_secs": 3600.0,
    "jsonl": None,  # JSONL event stream file
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
    ("runtime", "root"): "runtime_root",
    ("weight_policy", "require_policy"): "require_policy",
    ("weights", "interval_secs"): "interval_secs",
    ("weights", "max_submissions"): "max_submissions",
    (
        "launch",
        "require_full_provenance_for_broadcast",
    ): "require_full_provenance_for_broadcast",
    (
        "launch",
        "require_completed_launch_for_broadcast",
    ): "require_completed_launch_for_broadcast",
    ("launch", "approval_file"): "launch_approval_file",
    ("provenance", "mode"): "provenance",
    ("provenance", "evidence_url"): "evidence_url",
    ("provenance", "registry_keys"): "provenance_registry_keys",
    ("provenance", "registry_keys_digest"): "provenance_registry_keys_digest",
    ("provenance", "report_keys"): "provenance_report_keys",
    ("provenance", "report_keys_digest"): "provenance_report_keys_digest",
    ("provenance", "index_keys"): "provenance_index_keys",
    ("provenance", "index_keys_digest"): "provenance_index_keys_digest",
    ("provenance", "verifier_digest"): "provenance_verifier_digest",
    ("provenance", "mechanism"): "provenance_mechanism",
    ("provenance", "controlled_dir"): "provenance_controlled_dir",
    ("provenance", "verifier_binary"): "provenance_verifier_binary",
    ("provenance", "source_revision"): "provenance_source_revision",
    ("provenance", "burn_hotkey"): "provenance_burn_hotkey",
    ("logs", "jsonl"): "jsonl",
}

# env var -> our flat config key
_ENV_MAP = {
    "CATHEDRAL_PUBLISHER_URL": "publisher_url",
    "CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY": "public_key_hex",
    "CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX": "public_key_hex",  # legacy spelling
    "CATHEDRAL_WEIGHT_POLICY_KEY_ID": "key_id",
    "CATHEDRAL_WEIGHT_POLICY_NETWORK": "network",
    "CATHEDRAL_WEIGHT_POLICY_NETUID": "netuid",
    "BT_WALLET_NAME": "wallet_name",
    "BT_WALLET_HOTKEY": "wallet_hotkey",
    "CATHEDRAL_VALIDATOR_STATE": "state_file",
    "CATHEDRAL_VALIDATOR_REQUIRE_POLICY": "require_policy",
    "CATHEDRAL_VALIDATOR_PROVENANCE": "provenance",
    "CATHEDRAL_EVIDENCE_URL": "evidence_url",
    "CATHEDRAL_PROVENANCE_REGISTRY_KEYS": "provenance_registry_keys",
    "CATHEDRAL_PROVENANCE_REGISTRY_KEYS_DIGEST": "provenance_registry_keys_digest",
    "CATHEDRAL_PROVENANCE_REPORT_KEYS": "provenance_report_keys",
    "CATHEDRAL_PROVENANCE_REPORT_KEYS_DIGEST": "provenance_report_keys_digest",
    "CATHEDRAL_PROVENANCE_INDEX_KEYS": "provenance_index_keys",
    "CATHEDRAL_PROVENANCE_INDEX_KEYS_DIGEST": "provenance_index_keys_digest",
    "CATHEDRAL_PROVENANCE_VERIFIER_DIGEST": "provenance_verifier_digest",
    "CATHEDRAL_PROVENANCE_MECHANISM": "provenance_mechanism",
    "CATHEDRAL_PROVENANCE_CONTROLLED_DIR": "provenance_controlled_dir",
    "CATHEDRAL_PROVENANCE_VERIFIER_BINARY": "provenance_verifier_binary",
    "CATHEDRAL_PROVENANCE_SOURCE_REVISION": "provenance_source_revision",
    "CATHEDRAL_PROVENANCE_BURN_HOTKEY": "provenance_burn_hotkey",
    "CATHEDRAL_VALIDATOR_JSONL": "jsonl",
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


def _parse_bool(value: object, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{field} must be true or false")


def _resolve_serve_config(ns: argparse.Namespace) -> SimpleNamespace:
    cfg = dict(_DEFAULTS)
    if ns.config:
        cfg.update(_load_config_file(ns.config))
    for env, flat in _ENV_MAP.items():
        v = os.environ.get(env)
        if v:
            cfg[flat] = v
    # explicit flags win
    for flat in (
        "publisher_url",
        "public_key_hex",
        "key_id",
        "network",
        "netuid",
        "wallet_name",
        "wallet_hotkey",
        "state_file",
        "runtime_root",
        "interval_secs",
        "max_submissions",
        "require_full_provenance_for_broadcast",
        "require_completed_launch_for_broadcast",
        "launch_approval_file",
        "require_policy",
        "provenance",
        "evidence_url",
        "provenance_registry_keys",
        "provenance_registry_keys_digest",
        "provenance_report_keys",
        "provenance_report_keys_digest",
        "provenance_index_keys",
        "provenance_index_keys_digest",
        "provenance_verifier_digest",
        "provenance_mechanism",
        "provenance_controlled_dir",
        "provenance_verifier_binary",
        "provenance_source_revision",
        "provenance_burn_hotkey",
        "jsonl",
    ):
        v = getattr(ns, flat, None)
        if v is not None:
            cfg[flat] = v
    if ns.dry_run:
        cfg["broadcast"] = False
    elif getattr(ns, "broadcast", False):
        cfg["broadcast"] = True
    if ns.once:
        cfg["once"] = True
    if getattr(ns, "offline", False):
        cfg["offline"] = True
        cfg["broadcast"] = False
    cfg["netuid"] = int(cfg["netuid"])
    cfg["interval_secs"] = float(cfg["interval_secs"])
    cfg["max_submissions"] = int(cfg["max_submissions"])
    cfg["require_full_provenance_for_broadcast"] = _parse_bool(
        cfg["require_full_provenance_for_broadcast"],
        field="require_full_provenance_for_broadcast",
    )
    cfg["require_completed_launch_for_broadcast"] = _parse_bool(
        cfg["require_completed_launch_for_broadcast"],
        field="require_completed_launch_for_broadcast",
    )
    return SimpleNamespace(**cfg)


def _cmd_serve(ns: argparse.Namespace) -> int:
    cfg = _resolve_serve_config(ns)
    # Mirror validator_thin.main(): a --chain-endpoint flag populates the env the
    # resolver reads, so `serve` honors the override the same way (the env var
    # alone already works on this path; this makes the flag work too).
    if getattr(ns, "chain_endpoint", None):
        os.environ[validator_thin.CHAIN_ENDPOINT_ENV] = ns.chain_endpoint
    if not cfg.public_key_hex:
        print(
            "error: no signing key pinned. Set [weight_policy].public_key_hex in your "
            "config, or CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY, or --public-key-hex.\n"
            "It is the key published at "
            "https://api.cathedral.computer/.well-known/cathedral-jwks.json",
            file=sys.stderr,
        )
        return 2
    if (
        cfg.require_policy
        and cfg.require_policy not in validator_thin.REQUIRE_POLICY_CHOICES
    ):
        print(
            f"error: require_policy must be one of "
            f"{', '.join(validator_thin.REQUIRE_POLICY_CHOICES)}; got {cfg.require_policy!r}",
            file=sys.stderr,
        )
        return 2
    provenance_mode = getattr(cfg, "provenance", "shadow") or "shadow"
    if provenance_mode not in ("off", "shadow", "authority"):
        print(
            f"error: provenance must be off, shadow, or authority; got "
            f"{provenance_mode!r}",
            file=sys.stderr,
        )
        return 2
    try:
        validator_thin._validate_runtime_contract(cfg)
    except validator_thin.wire.VectorError as exc:
        print(
            f"error: {validator_thin.stable_error(exc)}",
            file=sys.stderr,
        )
        return 2
    mode = (
        "BROADCAST (setting weights)" if cfg.broadcast else "DRY-RUN (no chain writes)"
    )
    print(f"cathedral-validator serve — netuid {cfg.netuid} on {cfg.network} — {mode}")
    print(
        f"  publisher={validator_thin._safe_endpoint_label(cfg.publisher_url)}  "
        f"key_id={cfg.key_id}  pinned={cfg.public_key_hex[:16]}…"
    )
    if cfg.require_policy:
        print(f"  policy pin: {cfg.require_policy} (legacy/v3 vectors rejected)")
    authority_banner = {
        "off": "submission authority: THIN — provenance audit OFF",
        "shadow": "submission authority: THIN — provenance audits every tick "
        "(FULL only with controlled raw evidence; shadow)",
        "authority": "submission authority: FULL-PROVENANCE — the independent "
        "recomputation is what gets submitted",
    }[provenance_mode]
    print(f"  {authority_banner}")
    if getattr(cfg, "jsonl", None):
        print("  jsonl events → enabled (path withheld)")
    return validator_thin.run(cfg)


def _cmd_migrate(ns: argparse.Namespace) -> int:
    print(
        "nothing to migrate — v4 keeps no local validator database "
        "(scoring is composed and signed by the orchestrator)."
    )
    return 0


def _cmd_reconcile_launch(ns: argparse.Namespace) -> int:
    """Prove the recorded one-shot launch before unlocking the daemon."""
    cfg = _resolve_serve_config(ns)
    if getattr(ns, "chain_endpoint", None):
        os.environ[validator_thin.CHAIN_ENDPOINT_ENV] = ns.chain_endpoint
    try:
        result = validator_thin.reconcile_launch_transition(cfg)
    except Exception as exc:  # noqa: BLE001 - sanitized operator boundary
        print(
            f"launch reconciliation failed closed: {validator_thin.stable_error(exc)}",
            file=sys.stderr,
        )
        return 1
    print(
        "launch reconciliation PASS "
        f"block={result['block_number']} "
        f"extrinsic={result['extrinsic_hash']}"
    )
    return 0


def _cmd_preflight_launch(ns: argparse.Namespace) -> int:
    """Run the exact launch gate without a reservation, unlock, or write."""
    cfg = _resolve_serve_config(ns)
    cfg.launch_preflight = True
    cfg.broadcast = False
    cfg.offline = False
    cfg.once = True
    if getattr(ns, "chain_endpoint", None):
        os.environ[validator_thin.CHAIN_ENDPOINT_ENV] = ns.chain_endpoint
    approval_out = Path(getattr(ns, "approval_out", None) or cfg.launch_approval_file)
    try:
        result = validator_thin.run_launch_preflight(
            cfg,
            approval_out=approval_out,
        )
    except Exception as exc:  # noqa: BLE001 - sanitized operator boundary
        print(
            f"launch preflight failed closed: {validator_thin.stable_error(exc)}",
            file=sys.stderr,
        )
        return 1
    print(
        "launch preflight PASS "
        f"approval={result['approval_digest']} "
        f"valid_until_block={result['approval_valid_until_block']}"
    )
    return 0


def _cmd_version(ns: argparse.Namespace) -> int:
    from . import __version__

    print(f"cathedral-validator {__version__}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="cathedral-validator",
        description="Cathedral SN39 validator (v4) — fetch one signed score per miner, "
        "verify, apply.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser(
        "serve", help="run the validator (dry-run unless --broadcast is explicit)"
    )
    sp.add_argument(
        "--config",
        default=os.environ.get("CATHEDRAL_VALIDATOR_CONFIG"),
        help="path to a TOML config (e.g. config/validator.toml)",
    )
    sp.add_argument("--publisher-url", dest="publisher_url", default=None)
    sp.add_argument("--public-key-hex", dest="public_key_hex", default=None)
    sp.add_argument("--key-id", dest="key_id", default=None)
    sp.add_argument("--network", dest="network", default=None)
    sp.add_argument(
        "--chain-endpoint",
        dest="chain_endpoint",
        default=None,
        help="connect to your own subtensor RPC node (ws/wss URL) instead of the "
        "public entrypoint; the network label is kept for signing",
    )
    sp.add_argument("--netuid", dest="netuid", type=int, default=None)
    sp.add_argument("--wallet-name", dest="wallet_name", default=None)
    sp.add_argument("--wallet-hotkey", dest="wallet_hotkey", default=None)
    sp.add_argument("--state-file", dest="state_file", default=None)
    sp.add_argument(
        "--runtime-root",
        dest="runtime_root",
        default=None,
        help="absolute owner-only directory shared by thin and FULL processes "
        "for the cross-mode lock and ambiguity journal",
    )
    sp.add_argument("--interval-secs", dest="interval_secs", type=float, default=None)
    sp.add_argument(
        "--max-submissions",
        dest="max_submissions",
        type=int,
        default=None,
        help="optional local durable-attempt ceiling; 0 disables this extra "
        "ceiling, but SN39 recurring writes still require a separately signed "
        "bounded authorization; launch canary requires 1",
    )
    sp.add_argument(
        "--require-full-provenance-for-broadcast",
        dest="require_full_provenance_for_broadcast",
        action="store_true",
        default=None,
        help="launch-only: synchronously replay raw evidence and require exact "
        "agreement before the one permitted chain write",
    )
    sp.add_argument(
        "--require-completed-launch-for-broadcast",
        dest="require_completed_launch_for_broadcast",
        action="store_true",
        default=None,
        help="refuse continuous writes until reconcile-launch proves the finalized "
        "FULL-gated launch; on by default and mandatory for provenance=authority "
        "or any host holding the controlled launch material. A third-party "
        "validator that only relays Cathedral's signed vector opts out in its "
        "config file (see config/validator-thin-sn39-relay.toml)",
    )
    sp.add_argument(
        "--require-policy",
        dest="require_policy",
        default=None,
        help="pin the validator to a signed policy contract "
        "(confidential_primary_v1 or validated_supply_v1); "
        "legacy/v3 vectors are rejected",
    )
    sp.add_argument(
        "--provenance",
        dest="provenance",
        default=None,
        choices=("off", "shadow", "authority"),
        help="full-provenance mode: shadow (default) audits published "
        "evidence concurrently; authority submits the independent "
        "recomputation; off disables the audit",
    )
    sp.add_argument("--evidence-url", dest="evidence_url", default=None)
    sp.add_argument(
        "--provenance-registry-keys", dest="provenance_registry_keys", default=None
    )
    sp.add_argument(
        "--provenance-registry-keys-digest",
        dest="provenance_registry_keys_digest",
        default=None,
    )
    sp.add_argument(
        "--provenance-report-keys", dest="provenance_report_keys", default=None
    )
    sp.add_argument(
        "--provenance-report-keys-digest",
        dest="provenance_report_keys_digest",
        default=None,
    )
    sp.add_argument(
        "--provenance-index-keys", dest="provenance_index_keys", default=None
    )
    sp.add_argument(
        "--provenance-index-keys-digest",
        dest="provenance_index_keys_digest",
        default=None,
    )
    sp.add_argument(
        "--provenance-verifier-digest", dest="provenance_verifier_digest", default=None
    )
    sp.add_argument("--provenance-mechanism", dest="provenance_mechanism", default=None)
    sp.add_argument(
        "--provenance-controlled-dir", dest="provenance_controlled_dir", default=None
    )
    sp.add_argument(
        "--provenance-verifier-binary", dest="provenance_verifier_binary", default=None
    )
    sp.add_argument(
        "--provenance-source-revision", dest="provenance_source_revision", default=None
    )
    sp.add_argument(
        "--provenance-burn-hotkey", dest="provenance_burn_hotkey", default=None
    )
    sp.add_argument(
        "--jsonl",
        dest="jsonl",
        default=None,
        help="append the stable JSONL event stream to this file",
    )
    sp.add_argument(
        "--dry-run",
        action="store_true",
        help="verify and print the weights without setting them on chain",
    )
    sp.add_argument(
        "--broadcast",
        action="store_true",
        help="explicitly permit a chain weight submission",
    )
    sp.add_argument(
        "--offline",
        action="store_true",
        help="verify + print only, no chain access (CI / smoke)",
    )
    sp.add_argument("--once", action="store_true", help="single tick then exit")
    sp.set_defaults(func=_cmd_serve)

    mp = sub.add_parser("migrate", help="(no-op in v4 — kept for update-script parity)")
    mp.set_defaults(func=_cmd_migrate)

    rp = sub.add_parser(
        "reconcile-launch",
        help="verify the finalized FULL-gated launch and unlock continuous broadcast",
    )
    rp.add_argument("--config", required=True)
    rp.add_argument("--chain-endpoint", dest="chain_endpoint", default=None)
    rp.set_defaults(
        func=_cmd_reconcile_launch,
        dry_run=False,
        broadcast=False,
        once=False,
        offline=False,
    )

    pp = sub.add_parser(
        "preflight-launch",
        help="run the exact SN39 FULL launch gate read-only and emit approval",
    )
    pp.add_argument("--config", required=True)
    pp.add_argument("--chain-endpoint", dest="chain_endpoint", default=None)
    pp.add_argument("--approval-out", dest="approval_out", default=None)
    pp.set_defaults(
        func=_cmd_preflight_launch,
        dry_run=True,
        broadcast=False,
        once=True,
        offline=False,
    )

    vp = sub.add_parser("version", help="print version")
    vp.set_defaults(func=_cmd_version)

    ns = p.parse_args(argv)
    return ns.func(ns)


if __name__ == "__main__":
    raise SystemExit(main())
