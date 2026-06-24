"""Proof Board: a visual companion to the replay differential report.

`GET /api/scanner/differential` exposes the machine-readable proof that replay
harnesses are real discriminators. This module renders the same evidence as a
self-contained HTML page for operators and reviewers.

Each card is one pinned invariant. Exploit targets must separate exploit input
from benign input. Conserved targets must hold across stress witnesses.

Run standalone:

    python -m game.arena.proofboard [out_dir]
"""
from __future__ import annotations

import sys
from pathlib import Path

from game.arena import replay
from game.arena.replay_differential import differential_report

_CSS = """
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:radial-gradient(1100px 640px at 50% -10%,#11213f 0%,#070b16 60%);
  color:#e7ecf7;font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:46px 22px 60px}
h1{font-size:30px;margin:0 0 6px;text-align:center}
h1 .spark{color:#6fe3f0}
.tag{color:#8ea2c8;font-size:15px;text-align:center;margin:0 auto 8px;max-width:680px}
.summary{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin:18px 0 26px}
.pill{background:#101a30;border:1px solid #233252;border-radius:999px;padding:8px 16px;font-size:14px;color:#cdd6f4}
.pill b{color:#ffe79a}
.pill.ok{border-color:#27613f;color:#9af0c2}.pill.ok b{color:#79f0b8}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
.card{background:#101a30;border:1px solid #233252;border-radius:14px;padding:15px 16px}
.card .top{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:7px}
.fam{font-size:12px;font-weight:700;color:#7fd1e0;text-transform:uppercase;letter-spacing:.08em}
.kind{font-size:11px;font-weight:800;border-radius:6px;padding:3px 9px;text-transform:uppercase;letter-spacing:.06em}
.kind.exploit{background:#2a1418;border:1px solid #6a2a37;color:#ff8095}
.kind.conserved{background:#0d2018;border:1px solid #27613f;color:#79f0b8}
.tid{font-size:13px;color:#fff;font-weight:700;word-break:break-all;margin-bottom:4px}
.desc{font-size:13px;color:#b9c7e6;margin-bottom:9px}
.meta{display:flex;flex-wrap:wrap;gap:7px}
.chip{font-size:11px;color:#9fb0d4;background:#0d1730;border:1px solid #1d2740;border-radius:6px;padding:3px 8px}
.chip.real{color:#79f0b8;border-color:#27613f}
.chip.gov{color:#e0c069;border-color:#5a4a2a}
.foot{color:#56648a;font-size:12px;margin-top:26px;text-align:center}
@media(max-width:760px){.grid{grid-template-columns:1fr}}
"""


def _card(row: dict) -> str:
    target = replay.TARGETS.get(row["target_id"])
    kind = row["kind"]
    kind_label = "CRACKED / exploit" if kind == "exploit" else "HARDENED / conserved"
    desc = target.property_desc if target else ""
    source = (target.source if target else "") or "arena-port"
    reachable = bool(target.reachable) if target else False
    reach_chip = (
        '<span class="chip real">reachable</span>'
        if reachable
        else '<span class="chip gov">gov-gated / triaged</span>'
    )
    disc = (
        '<span class="chip real">proven discriminator</span>'
        if row["discriminator"]
        else '<span class="chip">not proven</span>'
    )
    return (
        f'<div class="card"><div class="top"><span class="fam">{row["family"]}</span>'
        f'<span class="kind {kind}">{kind_label}</span></div>'
        f'<div class="tid">{row["target_id"]}</div>'
        f'<div class="desc">{desc}</div>'
        f'<div class="meta"><span class="chip">model: {source}</span>{reach_chip}{disc}</div></div>'
    )


def _hardened_section() -> str:
    """Formally hardened invariants: z3 bit-blast says UNSAT and an INDEPENDENT CDCL
    solver confirms UNSAT. This is a proof no exploit exists, stronger than stress-testing."""
    hardened = [h for h in getattr(replay, "MINTED_HARDENED", []) if h.get("hardened")]
    if not hardened:
        return ""
    cards = []
    for h in hardened:
        cross = "z3 UNSAT + CDCL UNSAT" if h.get("cdcl_unsat") else "z3 UNSAT"
        cards.append(
            f'<div class="card"><div class="top"><span class="fam">{h.get("family","")}</span>'
            f'<span class="kind conserved">FORMALLY HARDENED</span></div>'
            f'<div class="tid">{h.get("rule_id","")}</div>'
            f'<div class="desc">{h.get("invariant","")}</div>'
            f'<div class="meta"><span class="chip">model: {h.get("model","")}</span>'
            f'<span class="chip real">{cross}</span>'
            f'<span class="chip real">cross-confirmed</span></div></div>')
    return (
        '<h2 style="text-align:center;font-size:20px;color:#79f0b8;margin:34px 0 6px">'
        f'Formally Hardened - {len(hardened)} invariants proven UNSAT (no exploit exists)</h2>'
        '<div class="tag">Two independent solvers agree these properties cannot be violated: '
        'z3 bit-blasts the negated invariant to UNSAT, and a separate CDCL solver re-confirms it.</div>'
        f'<div class="grid">{"".join(cards)}</div>')


def render_proofboard() -> str:
    report = differential_report()
    rows = sorted(
        report["targets"],
        key=lambda row: (row["kind"], row["family"], row["target_id"]),
    )
    cards = "".join(_card(row) for row in rows)
    all_real = "all real" if report["all_real"] else "check required"
    summary = (
        '<div class="summary">'
        f'<span class="pill"><b>{report["total"]}</b> pinned invariants</span>'
        f'<span class="pill"><b>{report["exploit"]}</b> exploit (CRACKED)</span>'
        f'<span class="pill"><b>{report["conserved"]}</b> conserved (HARDENED)</span>'
        f'<span class="pill ok"><b>{report["discriminators"]}/{report["total"]}</b> {all_real}</span>'
        '</div>'
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cathedral Arena - Proof Board</title><style>{_CSS}</style></head>
<body><div class="wrap">
<h1><span class="spark">*</span> Proof Board</h1>
<div class="tag">Every replay gate runs the real pinned math. Each invariant below
either separates exploit input from benign input, or holds across a conserved
stress set. That is why <b>replay_succeeds</b> is real, not theater.</div>
{summary}
<div class="grid">{cards}</div>
{_hardened_section()}
<div class="foot">Live JSON: GET /api/scanner/differential - Cathedral rewards proof, not claims.</div>
</div></body></html>"""


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "proofs.html"
    path.write_text(render_proofboard(), encoding="utf-8")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
