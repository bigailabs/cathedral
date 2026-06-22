"""Operator launch-readiness report for Cathedral v0.

Examples:
  python launch_readiness_report.py
  python launch_readiness_report.py --profile controlled-v0 --require-ready
  python launch_readiness_report.py --profile no-hardware-v0 --require-ready
  python launch_readiness_report.py --db /path/to/publisher.db --require-ready
  python launch_readiness_report.py --evidence-json launch_evidence.json --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from scaffold import launch_readiness as lr


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report Cathedral v0 launch readiness.")
    parser.add_argument("--db", help="publisher SQLite DB path to inspect for TEE GPU evidence")
    parser.add_argument("--evidence-json", help="JSON gate evidence or {'gates': {...}}")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--show-gates", action="store_true", help="include per-gate human output")
    parser.add_argument(
        "--profile",
        choices=("full", "controlled-v0", "no-hardware-v0", "intake-v0"),
        default="full",
        help="readiness profile: full requires real compute proof; no-hardware-v0 defers it",
    )
    parser.add_argument("--require-ready", action="store_true", help="exit non-zero unless selected profile is ready")
    args = parser.parse_args(argv)

    evidence = lr.local_scaffold_evidence()
    source = "local scaffold evidence"
    if args.db:
        from scaffold.publisher.store import Store

        evidence = lr.evidence_from_store(Store(args.db))
        source = f"publisher DB: {args.db}"
    if args.evidence_json:
        evidence.update(_load_evidence_json(args.evidence_json))
        source += f" + evidence JSON: {args.evidence_json}"

    profile = lr.normal_profile(args.profile)
    report = lr.evaluate(evidence, profile=profile)
    caveats = _evidence_caveats(profile=profile, used_db=bool(args.db))
    payload = {
        "source": source,
        "evidence": evidence,
        "report": report,
        "caveats": caveats,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_human(source, report, caveats=caveats, show_gates=args.show_gates)
    if args.require_ready and not report["ready"]:
        return 2
    return 0


def _load_evidence_json(path: str) -> dict[str, bool]:
    with Path(path).open("r", encoding="utf-8") as f:
        doc = json.load(f)
    if isinstance(doc, dict) and isinstance(doc.get("gates"), dict):
        doc = doc["gates"]
    if not isinstance(doc, dict):
        raise SystemExit("evidence JSON must be an object or {'gates': {...}}")
    known = {gate.id for gate in lr.gates()}
    unknown = sorted(set(doc) - known)
    if unknown:
        raise SystemExit(f"unknown launch gate ids: {', '.join(unknown)}")
    return {key: _strict_bool(key, value) for key, value in doc.items()}


def _strict_bool(key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise SystemExit(
        f"evidence gate {key!r} must be a JSON boolean true/false, "
        f"not {type(value).__name__}"
    )


def _evidence_caveats(*, profile: str, used_db: bool) -> list[str]:
    base = [
        "Full hardware-commissioning readiness still requires real verifier, provider, "
        "health, and usage/revenue proof.",
        "Provider, health, and usage receipts are operator-imported records; "
        "verify them against the external provider before miner hardware asks.",
        "A 100/100 report proves the DB has required receipts, not that Cathedral "
        "independently controls or audits the provider.",
        "Confirm CATHEDRAL_TEE_GPU_VERIFY_CMD is the real TDX plus NVIDIA GPU verifier, "
        "not a fixture; compare verifier_command_digest in receipts.",
        "Set CATHEDRAL_TEE_GPU_REAL_VERIFIER_DIGESTS to the approved verifier digest "
        "before treating DB evidence as production launch evidence.",
        "Set CATHEDRAL_TEE_GPU_INTAKE_CODE or CATHEDRAL_TEE_GPU_INTAKE_ALLOWLIST "
        "before accepting miner offers; without one, public intake fails closed.",
    ]
    if profile == "controlled-v0":
        base.insert(
            0,
            "Controlled v0 defers the real compute proof gates; Secure Compute is "
            "gated live intake plus operator review only.",
        )
    if not used_db:
        base.append("No publisher DB was inspected; this is local scaffold evidence only.")
    return base


def _print_human(
    source: str,
    report: dict[str, Any],
    *,
    caveats: list[str],
    show_gates: bool,
) -> None:
    ready_text = "READY" if report["ready"] else "NOT READY"
    print(f"Cathedral launch readiness: {ready_text}")
    print(f"Profile: {report.get('profile', 'full')}")
    print(f"Score: {report['score']:.1f}/{report['total']:.1f} ({report['percentage']:.1f}%)")
    print(f"Source: {source}")
    print("")
    print("Tiers:")
    for tier, item in report["by_tier"].items():
        print(f"  - {tier}: {item['achieved']:.1f}/{item['points']:.1f} ({item['percentage']:.1f}%)")
        if show_gates:
            for gate in item["gates"]:
                mark = "ok" if gate["ok"] else "missing"
                print(f"      {mark}: {gate['id']} ({gate['points']:.1f})")
    print("")
    if report["blockers"]:
        print("Blockers:")
        for blocker in report["blockers"]:
            print(f"  - {blocker['id']}: {blocker['needed_evidence']}")
    else:
        print("Blockers: none")
    if report.get("deferred"):
        print("")
        print("Deferred gates:")
        for gate in report["deferred"]:
            print(f"  - {gate['id']}: {gate['needed_evidence']}")
    print("")
    print("Evidence caveats:")
    for caveat in caveats:
        print(f"  - {caveat}")


if __name__ == "__main__":
    raise SystemExit(main())
