"""Provenance + growth dashboard — GRAPHICAL. Stdlib only (hand-rolled SVG, no
deps, no CDN). Shows movement and progress with clear affordances:

  * big status badges (GUARD provenance, LIVE TESTNET weight-set)
  * growth line chart over rounds
  * per-worker score bars (green = earning, red = exploit caught, grey = zero)
  * lane-throughput, gates-fired, and on-chain weight-vector bar charts

    python -m scaffold.harness 8       # produce data/harness_state.json
    python -m scaffold.dashboard       # serve on :8099 (re-reads state each refresh)
    python -m scaffold.dashboard --html
"""
from __future__ import annotations

import json
from pathlib import Path

STATE = Path("data/harness_state.json")
EVENTS = Path("data/events.jsonl")
OUT = Path("data/dashboard.html")

GREEN, RED, GREY, BLUE, AMBER = "#34c759", "#ff3b30", "#c7c7cc", "#0a84ff", "#ff9f0a"
_EXPLOIT = ("liar", "fraud", "vacuous", "crier", "missed", "unattest")


def _hbars(items, *, width=460, bar_h=20, gap=6, color=lambda k, v: GREEN,
           vmax=None, fmt=lambda v: f"{v:.3f}", lblw=150) -> str:
    items = list(items)
    if not items:
        return "<p style='color:#999'>(none)</p>"
    vmax = vmax or max((v for _, v in items), default=1) or 1
    H = len(items) * (bar_h + gap) + gap
    rows = []
    for i, (k, v) in enumerate(items):
        y = gap + i * (bar_h + gap)
        w = max(2.0, (v / vmax) * (width - lblw - 70)) if v > 0 else 2.0
        rows.append(
            f'<text x=0 y={y + bar_h * 0.72:.0f} font-size=12 fill="#222">{k}</text>'
            f'<rect x={lblw} y={y} width={w:.1f} height={bar_h} rx=3 fill="{color(k, v)}"/>'
            f'<text x={lblw + w + 6:.1f} y={y + bar_h * 0.72:.0f} font-size=11 fill="#666">{fmt(v)}</text>')
    return f'<svg width={width} height={H}>{"".join(rows)}</svg>'


def _line(points, *, width=460, height=150, color=BLUE, label="") -> str:
    if not points:
        return "<p style='color:#999'>(no rounds yet)</p>"
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    xmin, xmax = min(xs), (max(xs) or 1)
    ymax = max(ys) or 1

    def sx(x):
        return 34 + (x - xmin) / ((xmax - xmin) or 1) * (width - 50)

    def sy(y):
        return height - 22 - (y / ymax) * (height - 40)

    pts = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in points)
    dots = "".join(f'<circle cx={sx(x):.1f} cy={sy(y):.1f} r=3.5 fill="{color}"/>'
                   f'<text x={sx(x):.1f} y={sy(y) - 7:.1f} font-size=10 fill="#888" text-anchor=middle>{int(y)}</text>'
                   for x, y in points)
    xlbls = "".join(f'<text x={sx(x):.1f} y={height - 6:.0f} font-size=10 fill="#999" text-anchor=middle>{int(x)}</text>'
                    for x, _ in points)
    return (f'<svg width={width} height={height}>'
            f'<polyline points="{pts}" fill=none stroke="{color}" stroke-width=2.5/>{dots}{xlbls}'
            f'<text x=2 y=12 font-size=11 fill="#666">{label}</text></svg>')


def _feed(n=48) -> str:
    if not EVENTS.exists():
        return "<p style='color:#999'>(no events yet — run: <code>python -m scaffold.live</code>)</p>"
    lines = EVENTS.read_text().splitlines()[-n:]
    rows = []
    for ln in reversed(lines):
        try:
            e = json.loads(ln)
        except Exception:
            continue
        t = e.get("type")
        ts = f"<span class=ts>{e.get('ts','')}</span>"
        lane = f"<span class=ln>{e.get('lane','')}</span>" if e.get("lane") else ""
        w = e.get("worker", "")
        if t == "round":
            rows.append(f"<div class='ev sep'>── {e.get('msg')} ──</div>")
        elif t == "mint":
            rows.append(f"<div class=ev>{ts}<span class='tag b'>MINT</span>{lane} {e.get('msg')}</div>")
        elif t == "post":
            rows.append(f"<div class=ev>{ts}<span class='tag n'>POST</span>{lane} <b>{w}</b> submitted</div>")
        elif t == "verify":
            oc, sc, rs = e.get("outcome", "?"), e.get("score", 0), e.get("reason", "")
            cls = "ok" if sc and sc > 0 else ("bad" if oc == "invalid" else "warn")
            extra = f" · {rs}" if rs else ""
            rows.append(f"<div class=ev>{ts}<span class='tag {cls}'>{oc.upper()}</span>{lane} "
                        f"<b>{w}</b> → {sc}{extra}</div>")
        elif t == "consensus":
            fl = e.get("flag", "")
            cls = "bad" if fl == "outlier" else "ok"
            rows.append(f"<div class=ev>{ts}<span class='tag {cls}'>CONSENSUS</span>{lane} "
                        f"<b>{w}</b> {e.get('msg')}</div>")
        elif t == "weights":
            rows.append(f"<div class=ev>{ts}<span class='tag b'>WEIGHTS</span> {e.get('msg')}</div>")
        elif t == "cheat":
            rows.append(f"<div class=ev>{ts}<span class='tag bad'>CHEAT</span>{lane} {e.get('msg')}</div>")
        elif t == "error":
            rows.append(f"<div class=ev>{ts}<span class='tag warn'>ERROR</span> {e.get('msg')}</div>")
    return f"<div class=feed>{''.join(rows)}</div>"


def _worker_color(name, v):
    if v > 0:
        return GREEN
    return RED if any(e in name.lower() for e in _EXPLOIT) else GREY


# Static identity of each rail: what it IS, what it OPTIMIZES FOR, its CORE WORK.
# Merged with the live per-rail counters the runner emits in state["rails"].
RAILS = [
    ("sat_challenge_v1", "A", "SAT race", "fastest valid witness wins",
     "Miner solves a planted-SAT CNF and submits the assignment. The validator "
     "checks the witness against the formula — self-verifying, zero trust. Speed "
     "is the whole game: the bonus curve pays the fastest correct solve."),
    ("solver_docker_v1", "B", "Attested solve", "verifiable compute — vouch the unfalsifiable",
     "Miner runs a solver in a TDX container. SAT/UNSAT self-verify cheaply, but "
     "a TIMEOUT (\"I ran to the limit and it didn't close\") can't be checked "
     "offline — so that claim, and only that claim, is vouched by a hardware "
     "attestation of the actual run."),
    ("encoding_v1", "C", "Encoding · bug-finding", "witness quality — correct, fast, rare",
     "Miner encodes a public contract property to SMT and SOLVES for a triggering "
     "input — a real counterexample, which z3 independently re-checks. The fault "
     "only fires on a per-instance trigger, so a guessed constant earns nothing; "
     "you have to actually solve. Score = correctness + speed + trigger rarity."),
]


def _pill(label, value, color):
    return (f"<span class=pill style='border-color:{color}'>"
            f"<b style='color:{color}'>{value}</b> {label}</span>")


def _rail_cards(rails: dict) -> str:
    cards = []
    for fam, chip, name, optimizes, core in RAILS:
        r = rails.get(fam, {})
        finds = int(r.get("finds", 0))
        blocked = int(r.get("blocked", 0))
        safe = int(r.get("safe", 0))
        refuted = int(r.get("refuted", 0))
        mints = int(r.get("mints", 0))
        top = r.get("top", 0.0)
        desc = r.get("desc", "—")
        pills = (_pill("verified finds", finds, GREEN)
                 + _pill("caught / blocked", blocked, RED)
                 + _pill("safe·unrewarded", safe, GREY)
                 + (_pill("peer-refuted", refuted, AMBER) if refuted else "")
                 + _pill("challenges", mints, BLUE))
        cards.append(
            f"<div class=rail>"
            f"<div class=railhead><span class=chip>{chip}</span>"
            f"<span class=rname>{name}</span>"
            f"<span class=opt>optimizes for: <b>{optimizes}</b></span></div>"
            f"<div class=work>{core}</div>"
            f"<div class=now><span class=nowk>▶ live now</span> {desc}"
            f"<span class=top>top score this round: <b>{top}</b></span></div>"
            f"<div class=pills>{pills}</div>"
            f"</div>")
    return "".join(cards)


def render_html(s: dict) -> str:
    prov = s.get("provenance", {})
    mode = "BROADCAST" if prov.get("broadcast_enabled") else "DRY-RUN"
    guard = (f"<div class='badge guard'><span class=k>GUARD</span> "
             f"<b>{prov.get('validator_label')}</b> · {prov.get('repo')}@{prov.get('commit')} · "
             f"hotkey <code>{(prov.get('hotkey') or '')[:22]}</code> · netuid {prov.get('netuid')} "
             f"/ {prov.get('network')} · <b>{mode}</b></div>")

    tn = s.get("testnet_result")
    tn_html = ""
    if tn:
        tn_html = (f"<div class='badge ok'><span class=k>✅ LIVE ON-CHAIN</span> "
                   f"weights set · {tn.get('network')} netuid {tn.get('netuid')} · "
                   f"{tn.get('validator')} · uids {tn.get('uids')}<br>"
                   f"<b>{tn.get('landed')}</b></div>")

    # movement = per-round activity (not cumulative — those saturate and look flat)
    growth = s.get("growth", [])
    line_active = _line([(g["round"], g.get("active_round", 0)) for g in growth],
                        color=GREEN, label="workers earning THIS round")
    line_graded = _line([(g["round"], g.get("graded_round", 0)) for g in growth],
                        color=BLUE, label="validations THIS round")

    # per-worker score bars
    pw = sorted(s.get("per_worker_score", {}).items(), key=lambda kv: -kv[1])
    worker_bars = _hbars(pw, color=_worker_color, lblw=120)

    # weight vector (on-chain) bars
    wv = list(s.get("weight_vector_by_worker", {}).items())
    wv_bars = _hbars(wv, color=lambda k, v: BLUE, lblw=120, fmt=lambda v: f"{v:.3f}")

    # lane throughput + gates
    lane_bars = _hbars(s.get("lane_throughput", {}).items(),
                       color=lambda k, v: BLUE, fmt=lambda v: str(int(v)), lblw=160)
    gate_bars = _hbars(sorted(s.get("gates_fired", {}).items(), key=lambda kv: -kv[1]),
                       color=lambda k, v: AMBER, fmt=lambda v: str(int(v)), lblw=210)
    cons_bars = _hbars(s.get("consensus_flags", {}).items(),
                       color=lambda k, v: (GREEN if "find" in k else RED if "outlier" in k else GREY),
                       fmt=lambda v: str(int(v)), lblw=140)

    att = s.get("attest", {})
    att_html = (f"<div class=stat><div class=num>{att.get('live_verified', 0)}/{att.get('live_calls', 0)}</div>"
                f"<div class=lbl>attest verified</div></div>"
                f"<div class=stat><div class=num>${att.get('cost_usd', 0)}</div><div class=lbl>attest cost</div></div>"
                f"<div class=stat><div class=num>{s.get('rounds', 0)}</div><div class=lbl>rounds</div></div>"
                f"<div class=stat><div class=num>{s.get('workers', 0)}</div><div class=lbl>workers</div></div>")

    ln = s.get("learnings", {})
    ln_html = ""
    if ln:
        ch = ln.get("cheat", {})
        verdict = ln.get("cheat_verdict", "")
        cls = "ok" if "NO CHEAT" in verdict else "bad"
        ln_html = (f"<div class='badge {cls}'><span class=k>OVERNIGHT</span> "
                   f"{ln.get('uptime_rounds',0)} rounds / {ln.get('uptime_minutes',0)} min · "
                   f"{ln.get('total_validations',0)} validations · "
                   f"<b>cheat: {verdict}</b> "
                   f"(attempts {ch.get('attempts',0)} · blocked {ch.get('blocked',0)} · "
                   f"legit-finds {ch.get('legit_find',0)} · <b>bypass {ch.get('bypass',0)}</b>)"
                   f"<br>encoded so far: <code>{ln.get('mutations_encoded',{})}</code></div>")

    feed = _feed()
    rails = s.get("rails", {})
    rail_cards = _rail_cards(rails)

    # system target — the one thing the whole thing is doing, with the live tally
    tot_find = sum(int(r.get("finds", 0)) for r in rails.values())
    tot_block = sum(int(r.get("blocked", 0)) for r in rails.values())
    bypass = int(s.get("learnings", {}).get("cheat", {}).get("bypass", 0))
    bcls = "ok" if bypass == 0 else "bad"
    target = (
        "<div class=target>"
        "<div class=tk>SYSTEM TARGET</div>"
        "<div class=tt>Pay weight <b>only</b> for work it can independently verify.</div>"
        "<div class=td>Three rails each turn a different kind of compute into a "
        "<b>checkable artifact</b> — a SAT witness, an attested run, a triggering "
        "counterexample. Adversarial miners attack every round; weight flows to "
        "verified artifacts and to nothing else.</div>"
        f"<div class=tmetrics>"
        f"<span class=tm><b style='color:{GREEN}'>{tot_find}</b> verified artifacts paid</span>"
        f"<span class=tm><b style='color:{RED}'>{tot_block}</b> bad-faith attempts caught</span>"
        f"<span class='tm {bcls}'>cheated through: <b>{bypass}</b></span>"
        "</div></div>")

    def card(title, body):
        return f"<div class=card><h3>{title}</h3>{body}</div>"

    return f"""<!doctype html><meta charset=utf-8><title>Cathedral tripartite</title>
<meta http-equiv=refresh content=2>
<style>
 body{{font:14px/1.45 system-ui,-apple-system,sans-serif;margin:0;background:#f2f2f7;color:#1c1c1e}}
 .wrap{{max-width:1040px;margin:0 auto;padding:20px}}
 h2{{margin:0 0 4px}} .sub{{color:#8e8e93;margin:0 0 16px;font-size:13px}}
 .badge{{padding:10px 14px;border-radius:10px;margin-bottom:10px;font-size:13px}}
 .badge .k{{font-weight:700;margin-right:8px}}
 .guard{{background:#fff8e1;border:1px solid #ffd54f}}
 .ok{{background:#e7f8ee;border:1px solid {GREEN}}}
 .bad{{background:#fdeceb;border:1px solid {RED}}}
 .target{{background:#0b0f14;color:#e7ecf3;border-radius:12px;padding:16px 18px;margin-bottom:14px}}
 .target .tk{{font-size:11px;letter-spacing:.12em;color:#7aa2f7;font-weight:700}}
 .target .tt{{font-size:18px;margin:4px 0 6px}}
 .target .td{{font-size:13px;color:#aab4c2;max-width:760px}}
 .tmetrics{{margin-top:12px;display:flex;gap:10px;flex-wrap:wrap}}
 .tm{{background:#161b22;border:1px solid #2a313b;border-radius:8px;padding:6px 12px;font-size:13px}}
 .tm.bad{{border-color:{RED}}}
 .rails{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:14px}}
 .rail{{background:#fff;border-radius:12px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,.06);border-top:3px solid {BLUE}}}
 .railhead{{display:flex;align-items:center;gap:8px;margin-bottom:6px}}
 .chip{{background:{BLUE};color:#fff;font-weight:700;border-radius:6px;width:24px;height:24px;
        display:inline-flex;align-items:center;justify-content:center;font-size:13px}}
 .rname{{font-weight:700;font-size:15px}}
 .opt{{margin-left:auto;font-size:11px;color:#8e8e93;text-align:right;max-width:140px}}
 .work{{font-size:12px;color:#48484a;line-height:1.4;margin-bottom:8px}}
 .now{{font-size:12px;background:#f2f7ff;border-radius:8px;padding:7px 9px;margin-bottom:8px;color:#1c3a5e}}
 .now .nowk{{font-weight:700;color:{BLUE};margin-right:6px}}
 .now .top{{display:block;color:#5a6573;margin-top:3px}}
 .pills{{display:flex;flex-wrap:wrap;gap:5px}}
 .pill{{border:1px solid;border-radius:20px;padding:2px 9px;font-size:11px;color:#48484a}}
 .stats{{display:flex;gap:12px;margin:14px 0}}
 .stat{{background:#fff;border-radius:10px;padding:12px 18px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.06);flex:1}}
 .stat .num{{font-size:22px;font-weight:700;color:{BLUE}}} .stat .lbl{{font-size:11px;color:#8e8e93}}
 .grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
 .card{{background:#fff;border-radius:12px;padding:14px 16px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
 .card h3{{margin:0 0 8px;font-size:13px;color:#3a3a3c;text-transform:uppercase;letter-spacing:.04em}}
 code{{background:#f2f2f7;padding:1px 4px;border-radius:3px;font-size:12px}}
 .legend{{font-size:11px;color:#8e8e93;margin-top:4px}}
 .dot{{display:inline-block;width:9px;height:9px;border-radius:2px;margin:0 3px 0 8px;vertical-align:middle}}
 .feed{{font:12px/1.55 ui-monospace,Menlo,monospace;max-height:300px;overflow-y:auto;background:#0b0f14;color:#cdd6e0;border-radius:10px;padding:10px 12px}}
 .ev{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
 .ev .ts{{color:#5b6677;margin-right:8px}} .ev .ln{{color:#7aa2f7;margin:0 6px}}
 .ev.sep{{color:#5b6677;text-align:center;margin:5px 0}}
 .tag{{display:inline-block;min-width:74px;text-align:center;padding:0 6px;border-radius:4px;margin-right:8px;font-weight:700;font-size:10px}}
 .tag.ok{{background:#173d2a;color:{GREEN}}} .tag.bad{{background:#3d1717;color:#ff6b60}}
 .tag.warn{{background:#3d3417;color:{AMBER}}} .tag.b{{background:#15294d;color:#7aa2f7}}
 .tag.n{{background:#22262e;color:#9aa5b1}}
 .sec{{font-size:11px;color:#8e8e93;text-transform:uppercase;letter-spacing:.06em;margin:18px 0 6px}}
</style>
<div class=wrap>
<h2>Cathedral tripartite — live</h2>
<p class=sub>three rails · pay only for verified artifacts · auto-refresh 2s</p>
{target}
<div class=sec>The three rails — what each is, what it optimizes for, and what's happening inside it now</div>
<div class=rails>{rail_cards}</div>
{card("🟢 Live activity — every mint · post · validation · consensus call · weight update", feed)}
<div class=grid>
{card("Movement — activity per round (not cumulative)", line_active + line_graded)}
{card("Per-worker score <span class=legend><span class=dot style='background:{GREEN}'></span>earning<span class=dot style='background:{RED}'></span>exploit caught<span class=dot style='background:{GREY}'></span>zero</span>", worker_bars)}
{card("On-chain weight vector", wv_bars)}
{card("Gates fired (bad-faith caught)", gate_bars)}
{card("Lane throughput", lane_bars)}
{card("Consensus flags", cons_bars)}
</div>
{guard}{tn_html}{ln_html}
</div>"""


def write_html() -> Path:
    s = json.loads(STATE.read_text())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render_html(s))
    return OUT


def serve(port: int = 8099) -> None:
    import http.server
    import socketserver

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                html = render_html(json.loads(STATE.read_text())).encode()
            except Exception as e:
                html = f"<pre>no state yet: {e}</pre>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *a):
            pass

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server(("127.0.0.1", port), H) as httpd:
        print(f"dashboard at http://127.0.0.1:{port}")
        httpd.serve_forever()


if __name__ == "__main__":
    import sys
    if "--html" in sys.argv:
        print("wrote", write_html())
    else:
        serve()
