"""Shadow-diff machinery — milestone 4 (the migration's first rail).

v4 migrates through the SAME mechanism production uses: compute the v4 weight
vector ALONGSIDE the live production vector and record their per-UID difference in
a `shadow_weight_diffs`-shaped table, gated by the locked thresholds:

    |diff| >= 0.10  -> BLOCKER     (a flip would move real emission materially;
                                    do NOT advance the migration)
    |diff| >= 0.01  -> INVESTIGATE (a notable divergence to understand first)
    |diff|  < 0.01  -> OK          (within noise; safe)

This is computation + a table only — it NEVER sets weights. The scaffold
validator stays dry-run until Fred flips it (locked decision: weights ship
through the production Path B signed-vector pipeline). The shadow table is what a
human reads for the "~2 weeks green" gate before any blend.

Shape mirrors production's shadow_weight_diffs row: (uid, prod_weight,
shadow_weight, diff, band). Additive + idempotent: recomputing for the same
inputs yields the same rows.
"""
from __future__ import annotations

from dataclasses import dataclass

BLOCKER_THRESHOLD = 0.10
INVESTIGATE_THRESHOLD = 0.01


@dataclass(frozen=True)
class ShadowDiffRow:
    uid: int
    prod_weight: float
    shadow_weight: float
    diff: float           # shadow - prod
    band: str             # "blocker" | "investigate" | "ok"


def band_for(abs_diff: float) -> str:
    if abs_diff >= BLOCKER_THRESHOLD:
        return "blocker"
    if abs_diff >= INVESTIGATE_THRESHOLD:
        return "investigate"
    return "ok"


def compute_shadow_diffs(prod_by_uid: dict[int, float],
                         shadow_by_uid: dict[int, float]) -> list[ShadowDiffRow]:
    """Per-UID diff table over the UNION of UIDs present in either vector. A UID
    missing from one side is treated as weight 0 there (it gained/lost all its
    weight in the other regime). Rows are sorted by descending |diff| so the most
    material divergences read first."""
    uids = sorted(set(prod_by_uid) | set(shadow_by_uid))
    rows: list[ShadowDiffRow] = []
    for uid in uids:
        pw = round(prod_by_uid.get(uid, 0.0), 6)
        sw = round(shadow_by_uid.get(uid, 0.0), 6)
        diff = round(sw - pw, 6)
        rows.append(ShadowDiffRow(uid, pw, sw, diff, band_for(abs(diff))))
    rows.sort(key=lambda r: -abs(r.diff))
    return rows


@dataclass(frozen=True)
class ShadowVerdict:
    advance_ok: bool          # True iff NO blocker rows -> migration may proceed
    blockers: int
    investigate: int
    ok: int
    rows: list[ShadowDiffRow]

    def banner(self) -> str:
        status = "ADVANCE-OK" if self.advance_ok else "HOLD (blocker present)"
        return (f"[shadow-diff] {status} — blockers={self.blockers} "
                f"investigate={self.investigate} ok={self.ok}")


def evaluate(prod_by_uid: dict[int, float],
             shadow_by_uid: dict[int, float]) -> ShadowVerdict:
    """Roll the diff table into a go/no-go verdict. A single blocker row holds the
    migration (advance_ok=False) — exactly the production gate's behavior."""
    rows = compute_shadow_diffs(prod_by_uid, shadow_by_uid)
    blockers = sum(1 for r in rows if r.band == "blocker")
    investigate = sum(1 for r in rows if r.band == "investigate")
    ok = sum(1 for r in rows if r.band == "ok")
    return ShadowVerdict(advance_ok=blockers == 0, blockers=blockers,
                         investigate=investigate, ok=ok, rows=rows)


__all__ = ["ShadowDiffRow", "ShadowVerdict", "compute_shadow_diffs", "evaluate",
           "band_for", "BLOCKER_THRESHOLD", "INVESTIGATE_THRESHOLD"]
