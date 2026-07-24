"""Operator tool: ingest audit-family CNFs as shadow board challenges.

Default is dry-run and SAT-only. Current miner submit accepts SAT witnesses; UNSAT
proof intake is a later lane, so UNSAT instances are skipped unless explicitly
requested for smoke/visibility.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .app import seed_audit_challenge
from .store import Store


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [dict(x) for x in data]
    if isinstance(data, dict) and isinstance(data.get("instances"), list):
        return [dict(x) for x in data["instances"]]
    raise ValueError(f"unsupported manifest shape: {path}")


def _resolve_cnf(factory_out: Path, item: dict[str, Any]) -> Path | None:
    raw = item.get("file") or f"{item.get('cnf_id', '')}.cnf"
    p = Path(str(raw))
    candidates = [
        p,
        factory_out / p.name,
        factory_out.parent.parent / p,
    ]
    for c in candidates:
        if c.exists() and c.is_file():
            return c
    return None


def _load_decode_map(factory_out: Path, cnf_id: str) -> dict[str, Any] | None:
    p = factory_out / f"{cnf_id}.map.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _tier_for(item: dict[str, Any]) -> int:
    tier = str(item.get("tier", "")).lower()
    if tier in {"hard", "2", "tier2"}:
        return 2
    try:
        clauses = int(item.get("clauses") or 0)
    except Exception:
        clauses = 0
    return 2 if clauses >= 50_000 else 1


def ingest(
    *,
    db_path: str,
    factory_out: Path,
    manifests: list[Path],
    apply: bool,
    limit: int | None,
    include_unsat: bool,
    score_multiplier: float,
) -> list[dict[str, Any]]:
    store = Store(db_path)
    loaded: list[dict[str, Any]] = []
    try:
        for manifest_path in manifests:
            for item in _load_manifest(manifest_path):
                cnf_id = str(item.get("cnf_id") or "").strip()
                if not cnf_id:
                    continue
                result = str(item.get("verified_result") or item.get("result") or "").lower()
                if not include_unsat and not result.startswith("sat"):
                    continue
                cnf_path = _resolve_cnf(factory_out, item)
                if cnf_path is None:
                    loaded.append({"cnf_id": cnf_id, "status": "missing_cnf"})
                    continue
                challenge_id = "audit-" + cnf_id
                cnf_text = cnf_path.read_text(encoding="utf-8")
                decode_map = _load_decode_map(factory_out, cnf_id)
                row = {
                    "challenge_id": challenge_id,
                    "cnf_id": cnf_id,
                    "tier": _tier_for(item),
                    "result": result,
                    "cnf_path": str(cnf_path),
                    "score_multiplier": score_multiplier,
                }
                if apply:
                    seed_audit_challenge(
                        store,
                        challenge_id=challenge_id,
                        tier=row["tier"],
                        cnf_text=cnf_text,
                        manifest=item,
                        decode_map=decode_map,
                        source_path=str(cnf_path),
                        score_multiplier=score_multiplier,
                    )
                    row["status"] = "seeded"
                else:
                    row["status"] = "dry_run"
                loaded.append(row)
                if limit is not None and len([r for r in loaded if r["status"] != "missing_cnf"]) >= limit:
                    return loaded
        return loaded
    finally:
        store.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="SQLite path or Postgres DSN")
    ap.add_argument("--factory-out", default=r"C:\Users\fred\code\audit-hunter\factory\out")
    ap.add_argument("--manifest", action="append", default=None)
    ap.add_argument("--apply", action="store_true", help="actually seed rows; default is dry-run")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--include-unsat", action="store_true")
    ap.add_argument("--score-multiplier", type=float, default=0.0)
    args = ap.parse_args()

    factory_out = Path(args.factory_out)
    manifests = [Path(p) for p in args.manifest] if args.manifest else [
        factory_out / "manifest.json",
        factory_out / "manifest_root_reborn.json",
    ]
    rows = ingest(
        db_path=args.db,
        factory_out=factory_out,
        manifests=manifests,
        apply=bool(args.apply),
        limit=args.limit,
        include_unsat=bool(args.include_unsat),
        score_multiplier=float(args.score_multiplier),
    )
    print(json.dumps({"count": len(rows), "items": rows}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
