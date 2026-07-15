#!/usr/bin/env python3
"""Compare a live Cathedral env file against a non-secret launch template."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_PLACEHOLDER_RE = re.compile(r"^<secret(?::[^>]*)?>$")
MANAGED_PREFIXES = ("CATHEDRAL_", "RAILWAY_")
MANAGED_EXACT = {"DATABASE_URL", "PORT", "WEB_CONCURRENCY", "V2_GATE_MODE"}
SECRET_HINTS = (
    "DATABASE_URL",
    "DSN",
    "PASSWORD",
    "PRIVATE_KEY",
    "SIGNING_KEY",
    "SECRET",
    "TOKEN",
)


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


def _managed(key: str) -> bool:
    return key in MANAGED_EXACT or any(key.startswith(prefix) for prefix in MANAGED_PREFIXES)


def _redact(key: str, value: str | None) -> str:
    if value is None:
        return "<unset>"
    if SECRET_PLACEHOLDER_RE.match(value):
        return value
    if any(hint in key for hint in SECRET_HINTS):
        return "<set>" if value else "<missing>"
    return value if len(value) <= 96 else value[:93] + "..."


def check(template: dict[str, str], env: dict[str, str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    notes: list[str] = []

    for key in sorted(template):
        expected = template[key]
        actual = env.get(key)
        if actual is None or actual == "":
            errors.append(f"missing {key}")
            continue
        if SECRET_PLACEHOLDER_RE.match(expected):
            if SECRET_PLACEHOLDER_RE.match(actual):
                errors.append(f"{key} still has a secret placeholder")
            else:
                notes.append(f"secret-present {key}")
            continue
        if actual != expected:
            errors.append(
                f"{key}={_redact(key, actual)} expected {_redact(key, expected)}"
            )

    extras = sorted(key for key in env if _managed(key) and key not in template)
    for key in extras:
        errors.append(f"extra managed env {key}={_redact(key, env.get(key))}")

    return errors, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    args = parser.parse_args(argv)

    template = _parse_env_file(args.template)
    env = _parse_env_file(args.env_file)
    errors, notes = check(template, env)

    print("Cathedral env template check")
    print(f"template: {args.template}")
    print(f"env_file: {args.env_file}")
    print(f"template_keys: {len(template)}")
    print(f"managed_env_keys: {sum(1 for key in env if _managed(key))}")

    if notes:
        print()
        print("Secrets")
        for item in notes:
            print(f"  ok      {item}")

    if errors:
        print()
        print("Errors")
        for item in errors:
            print(f"  error   {item}")
        return 1

    print()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
