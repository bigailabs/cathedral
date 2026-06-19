#!/usr/bin/env python3
"""measure_inject.py — read live solve-time + solver-spread for the additive
inject lane (see scaffold/publisher/inject.py / INJECT.md) and compare it to the
native board over the same window.

Pure read. Touches no live state. Run it on the box (where DATABASE_URL points at
the prod Postgres) or against a SQLite copy:

    python measure_inject.py                          # PG via DATABASE_URL / CATHEDRAL_DB_PATH
    python measure_inject.py --db publisher.db        # explicit SQLite path
    python measure_inject.py --family gentest --window-hours 24

For each family it reports, per challenge: time-to-first-solve (first solve minus
mint) and distinct-solver count, then an aggregate (count, solved %, min/median/
mean time-to-first-solve, mean solvers). Injected vs native side by side answers:
do the unpredictable / harder injected instances solve slower, and who solves
them?
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from scaffold.publisher.store import Store

NATIVE_FAMILY = "synthetic_boolean_v1"


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _rows_for_family(store: Store, family: str, since_iso: str) -> list[dict]:
    """Per-challenge: mint time, first solve, distinct solver count, in window."""
    rows = store.query(
        "SELECT c.challenge_id AS cid, c.tier AS tier, c.status AS status, "
        "       c.created_at_iso AS created, "
        "       MIN(s.solved_at_iso) AS first_solved, "
        "       COUNT(DISTINCT s.miner_hotkey) AS n_solvers "
        "FROM lane_challenges c "
        "LEFT JOIN lane_challenge_solves s ON s.challenge_id = c.challenge_id "
        "WHERE c.family_id = ? AND c.created_at_iso > ? "
        "GROUP BY c.challenge_id, c.tier, c.status, c.created_at_iso",
        (family, since_iso))
    out = []
    for r in rows:
        created = _parse_iso(r["created"])
        first = _parse_iso(r["first_solved"])
        ttfs = (first - created).total_seconds() if (created and first) else None
        out.append({
            "cid": r["cid"], "tier": int(r["tier"]), "status": r["status"],
            "created": created, "ttfs": ttfs, "n_solvers": int(r["n_solvers"] or 0),
        })
    return out


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2


def _summary(rows: list[dict]) -> dict:
    solved = [r for r in rows if r["ttfs"] is not None]
    ttfs = [r["ttfs"] for r in solved]
    solvers = [r["n_solvers"] for r in rows]
    return {
        "challenges": len(rows),
        "solved": len(solved),
        "solved_pct": (100.0 * len(solved) / len(rows)) if rows else 0.0,
        "ttfs_min": min(ttfs) if ttfs else None,
        "ttfs_median": _median(ttfs),
        "ttfs_mean": (sum(ttfs) / len(ttfs)) if ttfs else None,
        "solvers_mean": (sum(solvers) / len(solvers)) if solvers else 0.0,
    }


def _fmt_secs(x: float | None) -> str:
    return "—" if x is None else (f"{x:.1f}s" if x < 120 else f"{x / 60:.1f}m")


def _print_family(label: str, rows: list[dict], examples: int) -> None:
    print(f"\n=== {label} ===")
    if not rows:
        print("  (no challenges in window)")
        return
    by_tier: dict[int, list[dict]] = {}
    for r in rows:
        by_tier.setdefault(r["tier"], []).append(r)
    for tier in sorted(by_tier):
        s = _summary(by_tier[tier])
        print(f"  tier {tier}: {s['challenges']} challenges, "
              f"{s['solved']} solved ({s['solved_pct']:.0f}%) | "
              f"time-to-first-solve min={_fmt_secs(s['ttfs_min'])} "
              f"median={_fmt_secs(s['ttfs_median'])} mean={_fmt_secs(s['ttfs_mean'])} | "
              f"mean distinct solvers={s['solvers_mean']:.1f}")
    if examples:
        print(f"  -- {min(examples, len(rows))} example challenges --")
        shown = sorted((r for r in rows if r["ttfs"] is not None),
                       key=lambda r: r["ttfs"])[:examples]
        for r in shown:
            print(f"     t{r['tier']} {r['cid'][:54]:54} "
                  f"ttfs={_fmt_secs(r['ttfs']):>7} solvers={r['n_solvers']}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=os.environ.get("CATHEDRAL_DB_PATH", "publisher.db"),
                   help="SQLite path, or a postgres DSN; DATABASE_URL overrides for PG")
    p.add_argument("--family", default="gentest", help="injected family_id to measure")
    p.add_argument("--native-family", default=NATIVE_FAMILY,
                   help="native family to compare against")
    p.add_argument("--window-hours", type=int, default=24,
                   help="only challenges minted within this window")
    p.add_argument("--examples", type=int, default=5,
                   help="example injected challenges to list (0 to suppress)")
    args = p.parse_args(argv)

    since = datetime.now(timezone.utc).timestamp() - args.window_hours * 3600
    since_iso = datetime.fromtimestamp(since, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")

    store = Store(args.db)
    print(f"DB backend: {store.backend} | window: last {args.window_hours}h "
          f"(since {since_iso})")

    inj = _rows_for_family(store, args.family, since_iso)
    nat = _rows_for_family(store, args.native_family, since_iso)
    _print_family(f"INJECTED  family='{args.family}'", inj, args.examples)
    _print_family(f"NATIVE    family='{args.native_family}'", nat, args.examples)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
