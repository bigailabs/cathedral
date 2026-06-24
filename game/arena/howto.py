"""Standalone "How to Play" page for the local Cathedral arena.

This is the short onboarding page for the playable game. It explains the loop
without turning into a static report or a product essay. Counts come from the real
arena corpus and verifier taxonomy so the page does not drift from the engine.
"""
from __future__ import annotations

import html
import sys
from pathlib import Path

from game.arena import corpus
from game.arena import reports
from game.arena.models import GateOutcome

_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#070b16;color:#e7ecf7;font:16px/1.55 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:920px;margin:0 auto;padding:30px 22px 60px}
h1{font-size:30px;margin:0 0 4px}
h1 .spark{color:#6fe3f0}
.tag{color:#8ea2c8;font-size:15px;margin:0 0 24px}
h2{font-size:19px;margin:32px 0 12px;color:#ffe79a}
.cards{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
.card{background:#101a30;border:1px solid #233252;border-radius:10px;padding:16px 18px}
.card .k{color:#7fd1e0;font-weight:700;font-size:12px;letter-spacing:.12em;text-transform:uppercase}
.card .t{font-weight:700;color:#ffe79a;font-size:17px;margin:7px 0 5px}
.card .d{font-size:15px;color:#d4ddf2}
.card .d b{color:#fff}
.rule{background:#0d1f1a;border:1px solid #27613f;border-radius:12px;padding:15px 18px;margin:18px 0;
  font-size:18px;text-align:center;color:#cfeede}
.rule b{color:#79f0b8}
.steps{counter-reset:s;margin:0;padding:0;list-style:none}
.steps li{counter-increment:s;position:relative;background:#0e1730;border:1px solid #1d2740;
  border-radius:10px;padding:13px 14px 13px 56px;margin:9px 0}
.steps li::before{content:counter(s);position:absolute;left:14px;top:12px;width:28px;height:28px;
  border-radius:50%;background:#16324a;color:#7fd1e0;font-weight:700;text-align:center;line-height:28px}
.steps li b{color:#fff}
.nets{display:flex;flex-wrap:wrap;gap:8px;margin:4px 0 2px}
.net{background:#10182b;border:1px solid #2a3a5e;border-radius:999px;padding:5px 12px;font-size:14px;color:#bcd0f0}
.note{color:#8ea2c8;font-size:14px;margin-top:6px}
.foot{color:#56648a;font-size:13px;margin-top:38px;text-align:center}
@media(max-width:680px){.cards{grid-template-columns:1fr}}
"""


def _esc(value: object) -> str:
    return html.escape(str(value))


def render_howto() -> str:
    cs = corpus.corpus_summary()
    targets = corpus.load_targets()
    n_targets = cs["targets"]
    n_gates = len(GateOutcome.GATES)
    n_axes = len(reports.ANTICHEAT_AXES)
    names = [t.name for t in targets if getattr(t, "name", None)][:8]
    nets = "".join(f'<span class="net">{_esc(n)}</span>' for n in names)

    cards = (
        '<div class="cards">'
        '<div class="card"><div class="k">Goal</div><div class="t">Break the right thing</div>'
        f'<div class="d">Pick one of <b>{n_targets}</b> subnet targets and look for a real '
        'incentive, scoring, accounting, or money-math failure.</div></div>'
        '<div class="card"><div class="k">Proof</div><div class="t">Reports do not score</div>'
        '<div class="d">A claim is useful only when the verifier can replay it. '
        '<b>Talk is free; proof pays.</b></div></div>'
        '<div class="card"><div class="k">Defense</div><div class="t">Hardening can win too</div>'
        '<div class="d">If a target cannot be broken, proving that cleanly is also valuable. '
        'The game rewards verified outcomes, not only exploits.</div></div>'
        '<div class="card"><div class="k">Gates</div><div class="t">Cheats pay zero</div>'
        f'<div class="d">The verifier checks ownership, freshness, replay, proof shape, and '
        f'attestation where required. It tracks <b>{n_axes}</b> anti-cheat classes.</div></div>'
        '</div>'
    )

    steps = (
        '<ol class="steps">'
        '<li><b>Probe.</b> Choose a target and inspect the task objective, risk, family, and reward.</li>'
        '<li><b>Encode.</b> Turn the suspected failure into a checkable proof task.</li>'
        '<li><b>Solve.</b> Produce the concrete input or evidence the verifier can test.</li>'
        '<li><b>Replay.</b> The verifier reruns the artifact. If it cannot reproduce, score is zero.</li>'
        '<li><b>Attest and seal.</b> Bind the accepted result to a receipt, then submit it for score.</li>'
        '</ol>'
    )

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>How to Play - Cathedral Arena</title><style>{_CSS}</style></head>
<body><div class="wrap">
<h1><span class="spark">*</span> How to Play Cathedral Arena</h1>
<div class="tag">A short guide to the playable proof loop.</div>

{cards}

<div class="rule">The rule: <b>you only win when Cathedral can verify the work.</b></div>

<h2>Round Loop</h2>
{steps}

<h2>Targets</h2>
<div class="nets">{nets}</div>
<div class="note">The live game routes across {n_targets} targets. Work is sandboxed; this local arena does not write to mainnet.</div>

<h2>Why Bad Submissions Fail</h2>
<div class="note">Every scored result must pass <b>{n_gates}</b> verifier gates. Missing proof, copied work,
wrong owner, stale nonce, fake attestation, invalid artifact, or failed replay makes the score <b>0</b>.</div>

<div class="foot">Cathedral rewards proof, not claims. Start the game at /game.</div>
</div></body></html>"""


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "howto.html"
    path.write_text(render_howto(), encoding="utf-8")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
