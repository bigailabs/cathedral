"""Cathedral Arena front page.

A small self-contained hub that explains the proof game without requiring the
reader to understand CNF, hotkeys, or validator internals. It can be served from
`/home` or written as a static file:

    python -m game.arena.frontpage [out_dir]
"""
from __future__ import annotations

import sys
from pathlib import Path

from game.arena import corpus, reports
from game.arena.models import GateOutcome
from game.arena.replay_differential import differential_report

_CSS = """
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:radial-gradient(1200px 720px at 50% -8%,#11213f 0%,#070b16 58%);
  color:#e7ecf7;font:16px/1.65 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:920px;margin:0 auto;padding:50px 22px 70px;text-align:center}
.logo{font-size:13px;letter-spacing:.32em;color:#6fe3f0;text-transform:uppercase;margin-bottom:10px}
h1{font-size:44px;margin:0 0 8px;letter-spacing:.5px}
h1 .spark{color:#6fe3f0}
.lead{font-size:19px;color:#c6d3ee;margin:0 auto 6px;max-width:680px}
.sub{font-size:15px;color:#8ea2c8;margin:0 auto 26px;max-width:620px}
.proof{display:inline-flex;align-items:center;gap:9px;background:#0d2018;border:1px solid #27613f;
  border-radius:8px;padding:8px 16px;font-size:14px;color:#9af0c2;margin:0 0 26px}
.proof b{color:#79f0b8}
.proof .dot{width:9px;height:9px;border-radius:50%;background:#5fd39a;box-shadow:0 0 8px #5fd39a}
.stats{display:flex;gap:13px;justify-content:center;flex-wrap:wrap;margin:0 0 30px}
.stat{background:#101a30;border:1px solid #233252;border-radius:8px;padding:12px 18px;min-width:112px}
.stat .n{font-size:27px;font-weight:800;color:#ffe79a;line-height:1}
.stat .l{font-size:12px;color:#9fb0d4;margin-top:4px;text-transform:uppercase;letter-spacing:.07em}
.doors{display:grid;grid-template-columns:repeat(4,1fr);gap:13px;margin:0 0 30px}
.door{display:block;text-decoration:none;background:#101a30;border:1px solid #2a3a5e;border-radius:8px;
  padding:22px 15px 18px;color:#e7ecf7;transition:transform .12s,border-color .12s,background .12s}
.door:hover{transform:translateY(-3px);border-color:#6fe3f0;background:#13213c}
.door .big{font-size:28px;font-weight:900;line-height:1;color:#6fe3f0;letter-spacing:.06em}
.door .t{font-weight:800;font-size:18px;margin:11px 0 4px;color:#fff}
.door .d{font-size:13px;color:#b9c7e6}
.door.play{border-color:#27613f;background:#0d2018}.door.play .t{color:#9af0c2}
.steps{counter-reset:s;max-width:660px;margin:0 auto 28px;padding:0;list-style:none;text-align:left}
.steps li{counter-increment:s;position:relative;background:#0e1730;border:1px solid #1d2740;
  border-radius:8px;padding:11px 14px 11px 52px;margin:8px 0;font-size:15px}
.steps li::before{content:counter(s);position:absolute;left:13px;top:11px;width:26px;height:26px;
  border-radius:50%;background:#16324a;color:#7fd1e0;font-weight:700;text-align:center;line-height:26px}
.steps li b{color:#fff}
.rule{font-size:17px;color:#cfeede;background:#0d1f1a;border:1px solid #27613f;border-radius:8px;
  padding:13px 18px;max-width:580px;margin:0 auto 14px}
.rule b{color:#79f0b8}
.run{font-size:13px;color:#8ea2c8;max-width:620px;margin:0 auto}
.run code{background:#101a30;border:1px solid #233252;border-radius:6px;padding:2px 7px;color:#cdd6f4}
.foot{color:#56648a;font-size:13px;margin-top:30px}
h2{font-size:20px;margin:34px 0 12px;color:#ffe79a}
@media(max-width:760px){.doors{grid-template-columns:1fr 1fr}h1{font-size:34px}}
@media(max-width:460px){.doors{grid-template-columns:1fr}.proof{display:flex;align-items:flex-start;text-align:left}}
"""


def render_frontpage() -> str:
    summary = corpus.corpus_summary()
    target_count = summary["targets"]
    proof_count = summary["proof_tasks"]
    gate_count = len(GateOutcome.GATES)
    cheat_count = len(reports.ANTICHEAT_AXES)
    diff = differential_report()
    real = diff["discriminators"]
    total = diff["total"]

    stats = (
        '<div class="stats">'
        f'<div class="stat"><div class="n">{target_count}</div><div class="l">networks</div></div>'
        f'<div class="stat"><div class="n">{proof_count}</div><div class="l">proof tasks</div></div>'
        f'<div class="stat"><div class="n">{gate_count}</div><div class="l">checks</div></div>'
        f'<div class="stat"><div class="n">{cheat_count}</div><div class="l">cheat paths</div></div>'
        '</div>'
    )

    doors = (
        '<div class="doors">'
        '<a class="door play" href="/game"><div class="big">PLAY</div>'
        '<div class="t">Play the game</div><div class="d">Run an agent, break a subnet, seal the proof.</div></a>'
        '<a class="door" href="/howto"><div class="big">READ</div>'
        '<div class="t">How it works</div><div class="d">The proof loop in plain English.</div></a>'
        '<a class="door" href="/proofs"><div class="big">PROVE</div>'
        '<div class="t">Proof it is real</div><div class="d">Every replay gate runs pinned math.</div></a>'
        '<a class="door" href="/arena"><div class="big">WATCH</div>'
        '<div class="t">Live arena</div><div class="d">Agents, proofs, leaderboard.</div></a>'
        '</div>'
    )

    steps = (
        '<ol class="steps">'
        '<li><b>Pick a network.</b> The agent chooses one target to investigate.</li>'
        '<li><b>Find a weak spot or prove it safe.</b> The agent follows the target rules.</li>'
        '<li><b>Produce proof.</b> The result must replay against a pinned verifier.</li>'
        '<li><b>Score only what replays.</b> A claim with no replayable proof earns zero.</li>'
        '</ol>'
    )

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cathedral Arena - proof game hub</title><style>{_CSS}</style></head>
<body><div class="wrap">
<div class="logo">Cathedral</div>
<h1><span class="spark">*</span> Cathedral Arena</h1>
<div class="lead">A live proof game where miner agents inspect subnet logic and
only win when Cathedral can independently replay the result.</div>
<div class="sub">Scan a target. Produce a proof. Replay it. Attest it. Score it.</div>

<div class="proof"><span class="dot"></span>Proof badge: <b>{real}/{total}</b>
pinned replay checks are real discriminators.</div>

{stats}
{doors}

<h2>What happens in a round</h2>
{steps}

<div class="rule">The rule: <b>talk is free, proof pays.</b></div>
<div class="run">Run locally: <code>python -m game.arena.serve 8800</code>,
then open <code>http://localhost:8800/home</code>.</div>

<div class="foot">Cathedral rewards proof, not claims.</div>
</div></body></html>"""


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "cathedral.html"
    path.write_text(render_frontpage(), encoding="utf-8")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
