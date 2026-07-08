#!/usr/bin/env python3
"""Audit the small Cathedral V2 relaunch environment surface.

The app still knows many historical CATHEDRAL_* toggles. For relaunch, operators
should set only the profile, role, shared store/secrets, and a few runtime
guardrails. This checker makes that rule executable for local env files,
systemd/Railway exports, and handoff reviews.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


CORE_REQUIRED = {
    "DATABASE_URL": "shared Postgres store for scoring and V2 receipts",
    "CATHEDRAL_LAUNCH_PROFILE": "must be v2-converged for the unified V2 miner path",
    "CATHEDRAL_SERVICE_ROLE": "explicit process role: all, read, submit, or worker",
    "CATHEDRAL_V2_VERIFY_WORKER_ENABLED": "explicit singleton verifier switch for this process",
    "CATHEDRAL_EVAL_SIGNING_KEY": "pinned Ed25519 signing key validators trust",
    "CATHEDRAL_V2_SUBMIT_TOKEN_SECRET": "HMAC secret for lazy CNF submit tokens",
    "CATHEDRAL_PERMINER_SEED_SECRET": "stable deterministic per-miner instance seed",
    "CATHEDRAL_CNF_TOKEN_SECRET": "legacy V1 CNF token secret until origin V1 removal",
    "CATHEDRAL_PG_STATEMENT_TIMEOUT_MS": "read-serving query guardrail; use 4000 on public origins",
    "CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE": "identity alignment / sybil hardening",
}

CORE_OPTIONAL = {
    # Runtime guardrails that actually move launch safety.
    "CATHEDRAL_PM_READ_HARD_CAP",
    "CATHEDRAL_V2_READ_THREADS",
    "CATHEDRAL_V2_SUBMIT_BITSET_THREADS",
    "CATHEDRAL_V2_VERIFY_BATCH_SIZE",
    "CATHEDRAL_V2_VERIFY_INTERVAL_SECS",
    "CATHEDRAL_V2_VERIFY_LOCK_SECS",
    "CATHEDRAL_V2_VERIFY_MAX_BLOB_BYTES",
    "CATHEDRAL_PG_POOL_MIN",
    "CATHEDRAL_PG_POOL_MAX",
    "CATHEDRAL_PG_CONNECT_TIMEOUT",
    "CATHEDRAL_THREADPOOL_TOKENS",
    # Mechanism calibration. Leave default unless intentionally changing SAT economics.
    "CATHEDRAL_PERMINER_EPOCH_BUCKET_HOURS",
    "CATHEDRAL_PERMINER_ALLOTMENT_T1",
    "CATHEDRAL_PERMINER_ALLOTMENT_T2",
    "CATHEDRAL_PERMINER_NVARS_T1",
    "CATHEDRAL_PERMINER_NVARS_T2",
    "CATHEDRAL_PERMINER_NCLAUSES_T1",
    "CATHEDRAL_PERMINER_NCLAUSES_T2",
    "CATHEDRAL_PERMINER_METHOD_T1",
    "CATHEDRAL_PERMINER_METHOD_T2",
    "CATHEDRAL_PERMINER_WEIGHT_T1",
    "CATHEDRAL_PERMINER_WEIGHT_T2",
    "CATHEDRAL_PERMINER_MAX_PAGE_LIMIT",
    "CATHEDRAL_V2_REAL_FRACTION",
    # Weight-vector identity / operator access.
    "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY",
    "CATHEDRAL_WEIGHT_POLICY_KEY_ID",
    "CATHEDRAL_WEIGHT_POLICY_NETWORK",
    "CATHEDRAL_WEIGHT_POLICY_NETUID",
    "CATHEDRAL_WEIGHT_POLICY_VALID_FOR_SECS",
    "CATHEDRAL_PUBLIC_BASE_URL",
    "CATHEDRAL_PUBLISHER_ADMIN_TOKEN",
    "CATHEDRAL_V2_ADMIN_TOKEN",
    # Local/container plumbing.
    "CATHEDRAL_DB_PATH",
    "PORT",
}

PROFILE_IMPLIED_FLAGS = {
    "CATHEDRAL_V2_ENABLED",
    "CATHEDRAL_V2_SUBMIT_BITSET_ENABLED",
    "CATHEDRAL_V2_LAZY_ISSUANCE",
    "CATHEDRAL_V2_PM_PAYOUT_BRIDGE",
    "CATHEDRAL_V2_PERMINER_ENABLED",
}

DANGEROUS_DEPRECATED = {
    "CATHEDRAL_V2_DATABASE_URL": "split V2 DB makes bridged payouts invisible to scoring",
    "CATHEDRAL_V2_DB_PATH": "split V2 DB makes bridged payouts invisible to scoring",
    "CATHEDRAL_PERMINER_ENABLED": "truthy value enables the retired V1 per-miner surface",
}

LEGACY_WARN_EXACT = {
    "CATHEDRAL_V2_PERMINER_ENV_PIN": "redundant: v2-converged pins the env bridge automatically",
    "CATHEDRAL_V2_PERMINER_SEED_SECRET": "prefer CATHEDRAL_PERMINER_SEED_SECRET",
    "CATHEDRAL_ASYNC_VERIFY_ENABLED": "old V1 async worker flag; V2 uses CATHEDRAL_V2_VERIFY_WORKER_ENABLED",
    "CATHEDRAL_PM_SUBMIT_ASYNC_ENABLED": "old V1 pm-async rollout flag; V2 bitset submit owns relaunch path",
    "CATHEDRAL_SUBMIT_ASYNC_ENABLED": "old public V1 async rollout flag; leave unset for V2 relaunch",
}

V2_PM_ENV_TWINS = {
    "CATHEDRAL_PERMINER_SEED_SECRET": "CATHEDRAL_V2_PERMINER_SEED_SECRET",
    "CATHEDRAL_PERMINER_EPOCH_HOURS": "CATHEDRAL_V2_PERMINER_EPOCH_HOURS",
    "CATHEDRAL_PERMINER_MAX_PAGE_LIMIT": "CATHEDRAL_V2_PERMINER_MAX_PAGE_LIMIT",
    "CATHEDRAL_PERMINER_ALLOTMENT_T1": "CATHEDRAL_V2_PERMINER_ALLOTMENT_T1",
    "CATHEDRAL_PERMINER_ALLOTMENT_T2": "CATHEDRAL_V2_PERMINER_ALLOTMENT_T2",
    "CATHEDRAL_PERMINER_WEIGHT_T1": "CATHEDRAL_V2_PERMINER_WEIGHT_T1",
    "CATHEDRAL_PERMINER_WEIGHT_T2": "CATHEDRAL_V2_PERMINER_WEIGHT_T2",
    "CATHEDRAL_PERMINER_METHOD_T1": "CATHEDRAL_V2_PERMINER_METHOD_T1",
    "CATHEDRAL_PERMINER_METHOD_T2": "CATHEDRAL_V2_PERMINER_METHOD_T2",
    "CATHEDRAL_PERMINER_NVARS_T1": "CATHEDRAL_V2_PERMINER_NVARS_T1",
    "CATHEDRAL_PERMINER_NVARS_T2": "CATHEDRAL_V2_PERMINER_NVARS_T2",
    "CATHEDRAL_PERMINER_NCLAUSES_T1": "CATHEDRAL_V2_PERMINER_NCLAUSES_T1",
    "CATHEDRAL_PERMINER_NCLAUSES_T2": "CATHEDRAL_V2_PERMINER_NCLAUSES_T2",
}

LEGACY_WARN_PREFIXES = (
    "CATHEDRAL_V2_PERMINER_",
    "CATHEDRAL_V2_SHADOW_V1_",
    "CATHEDRAL_EXTERNAL_SCORES_",
    "CATHEDRAL_TEE_GPU_",
    "CATHEDRAL_ARENA_",
    "CATHEDRAL_RETENTION_",
    "CATHEDRAL_SAT_GENERATOR_",
)

ALLOWED_GENERIC = {"DATABASE_URL", "PORT", "V2_GATE_MODE", "CLOUDFLARE_API_TOKEN"}
TRUTHY = {"1", "true", "yes", "on"}
FALSY = {"0", "false", "no", "off"}
PLACEHOLDER_RE = re.compile(
    r"^(|<.*>|change-?me|replace-?me|todo|xxx+|.*USER:PASSWORD@HOST.*)$",
    re.I,
)
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_HINTS = (
    "DATABASE_URL",
    "DSN",
    "PASSWORD",
    "PRIVATE_KEY",
    "SIGNING_KEY",
    "SECRET",
    "TOKEN",
)


def _is_present(value: str | None) -> bool:
    return value is not None and not PLACEHOLDER_RE.match(value.strip())


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUTHY


def _is_falsy(value: str | None) -> bool:
    return (value or "").strip().lower() in FALSY


def _redact(name: str, value: str | None) -> str:
    if value is None:
        return "<unset>"
    if any(hint in name for hint in SECRET_HINTS):
        return "<set>" if _is_present(value) else "<missing>"
    shown = value.strip()
    return shown if len(shown) <= 80 else shown[:77] + "..."


def _parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if raw.startswith("export "):
            raw = raw[len("export "):].strip()
        if "=" not in raw:
            print(f"warn: {path}:{lineno}: ignored line without '='", file=sys.stderr)
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not KEY_RE.match(key):
            print(f"warn: {path}:{lineno}: ignored invalid env name {key!r}", file=sys.stderr)
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        data[key] = value
    return data


def _interesting(env: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in sorted(env.items())
        if key.startswith("CATHEDRAL_") or key in ALLOWED_GENERIC
    }


def audit(env: dict[str, str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    profile = (env.get("CATHEDRAL_LAUNCH_PROFILE") or "").strip()
    role = (env.get("CATHEDRAL_SERVICE_ROLE") or "").strip().lower()

    for name, why in CORE_REQUIRED.items():
        if not _is_present(env.get(name)):
            errors.append(f"missing {name}: {why}")

    if profile and profile != "v2-converged":
        errors.append("CATHEDRAL_LAUNCH_PROFILE must be v2-converged for relaunch")

    if role and role not in {"all", "read", "submit", "worker"}:
        errors.append("CATHEDRAL_SERVICE_ROLE must be one of: all, read, submit, worker")

    db = env.get("DATABASE_URL", "").strip()
    if db and not db.startswith(("postgres://", "postgresql://")):
        errors.append("DATABASE_URL must be a postgres:// or postgresql:// DSN")

    timeout_raw = env.get("CATHEDRAL_PG_STATEMENT_TIMEOUT_MS")
    if role in {"all", "read"} and _is_present(timeout_raw):
        try:
            timeout_ms = int(str(timeout_raw).strip())
        except ValueError:
            errors.append("CATHEDRAL_PG_STATEMENT_TIMEOUT_MS must be a positive integer")
        else:
            if timeout_ms <= 0:
                errors.append("CATHEDRAL_PG_STATEMENT_TIMEOUT_MS must be > 0 on read-serving roles")

    if _is_present(env.get("CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE")):
        if not _is_truthy(env.get("CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE")):
            warnings.append("CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE is set but not truthy")

    if _is_present(env.get("CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY")):
        warnings.append(
            "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY overrides CATHEDRAL_EVAL_SIGNING_KEY for weights; "
            "only keep it if validators already pin that weight key"
        )

    for name in sorted(PROFILE_IMPLIED_FLAGS):
        if name not in env:
            continue
        if _is_falsy(env.get(name)):
            errors.append(f"{name} is explicitly off but v2-converged implies it on")
        else:
            warnings.append(f"{name} is redundant under CATHEDRAL_LAUNCH_PROFILE=v2-converged")

    for name, why in sorted(DANGEROUS_DEPRECATED.items()):
        if name not in env:
            continue
        if name == "CATHEDRAL_PERMINER_ENABLED" and not _is_truthy(env.get(name)):
            warnings.append(f"{name} is set; remove it entirely ({why})")
        else:
            errors.append(f"{name} must not be set for relaunch: {why}")

    for canonical, v2_twin in sorted(V2_PM_ENV_TWINS.items()):
        if canonical not in env or v2_twin not in env:
            continue
        if env[canonical] != env[v2_twin]:
            errors.append(
                f"{canonical} and {v2_twin} conflict; remove the V2 twin or "
                "make it identical so startup env pinning can proceed"
            )

    known = set(CORE_REQUIRED) | CORE_OPTIONAL | PROFILE_IMPLIED_FLAGS | set(DANGEROUS_DEPRECATED)
    known |= set(LEGACY_WARN_EXACT) | ALLOWED_GENERIC
    for name, value in _interesting(env).items():
        if name in known:
            continue
        if any(name.startswith(prefix) for prefix in LEGACY_WARN_PREFIXES):
            warnings.append(f"{name} is outside the relaunch core; leave unset unless reviewing that subsystem")
            continue
        warnings.append(f"{name} is not in the relaunch env surface (value={_redact(name, value)})")

    for name, why in sorted(LEGACY_WARN_EXACT.items()):
        if name in env:
            warnings.append(f"{name}: {why}")

    return errors, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        action="append",
        type=Path,
        help="Read KEY=value lines from a deploy env file. Repeatable.",
    )
    parser.add_argument(
        "--include-process-env",
        action="store_true",
        help="Merge the current process environment before env-file overlays.",
    )
    parser.add_argument(
        "--warnings-as-errors",
        action="store_true",
        help="Return failure when non-core envs or deprecated settings are present.",
    )
    args = parser.parse_args(argv)

    env: dict[str, str] = dict(os.environ) if (args.include_process_env or not args.env_file) else {}
    for env_file in args.env_file or []:
        env.update(_parse_env_file(env_file))

    interesting = _interesting(env)
    errors, warnings = audit(interesting)

    print("Cathedral V2 relaunch env audit")
    print(f"source: {'process env' if not args.env_file else ', '.join(str(p) for p in args.env_file)}")
    print(f"visible envs: {len(interesting)}")
    print()
    print("Core required")
    for name in sorted(CORE_REQUIRED):
        status = "ok" if _is_present(interesting.get(name)) else "missing"
        print(f"  {status:7} {name:44} {_redact(name, interesting.get(name))}")

    optional_set = sorted(name for name in CORE_OPTIONAL if name in interesting)
    if optional_set:
        print()
        print("Core optional set")
        for name in optional_set:
            print(f"  ok      {name:44} {_redact(name, interesting.get(name))}")

    if warnings:
        print()
        print("Warnings")
        for item in warnings:
            print(f"  warn    {item}")

    if errors:
        print()
        print("Errors")
        for item in errors:
            print(f"  error   {item}")

    if errors or (warnings and args.warnings_as_errors):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
